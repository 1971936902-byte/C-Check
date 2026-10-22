import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cli'))
from ccheck.core import DeploymentError
from ccheck.engine import render_compose
from ccheck.gpu import access_mode, cdi_device, docker_arguments, probe, validate_inspection


class GpuTests(unittest.TestCase):
    def site(self, gpu='GPU-abcd-1234', mode='cdi'):
        return {'gpu': gpu, 'gpu_access_mode': mode, 'instance': 'gpu-test',
                'data_dir': '/srv/c-check-test', 'profile': 'ada-48g-fp8',
                'web': {'bind': '127.0.0.1', 'port': 18800, 'public_url': 'http://127.0.0.1:18800'}}

    def inspection(self, site):
        return {'HostConfig': {'Runtime': 'runc', 'Devices': None, 'DeviceRequests': [
            {'Driver': 'cdi', 'DeviceIDs': [cdi_device(site)], 'Count': 0, 'Capabilities': None, 'Options': None}]},
            'Config': {'Env': ['NVIDIA_VISIBLE_DEVICES=void']}}

    def test_single_index_and_uuid_conversion(self):
        for gpu in ['0', '3', 'GPU-abcd-1234']:
            site = self.site(gpu)
            self.assertEqual(cdi_device(site), 'nvidia.com/gpu=' + gpu)
            self.assertIn(cdi_device(site), docker_arguments(site))
            self.assertNotIn('--gpus', docker_arguments(site))

    def test_reject_all_multiple_and_unqualified_inputs(self):
        for gpu in ['all', '0,1', 'nvidia.com/gpu=0', '', '../0']:
            with self.subTest(gpu=gpu), self.assertRaises(DeploymentError):
                cdi_device(self.site(gpu))

    def test_unknown_mode_fails_and_legacy_is_compatible(self):
        with self.assertRaises(DeploymentError):
            access_mode(self.site(mode='auto'))
        self.assertEqual(docker_arguments(self.site('2', 'legacy')), ['--gpus', 'device=2'])
        self.assertEqual(access_mode({}), 'legacy')

    def test_compose_cdi_only_on_api_and_qwen_preserves_isolation(self):
        images = {name: 'sha256:' + 'a' * 64 for name in
                  ['backend', 'frontend', 'qwen', 'embedding', 'mysql', 'redis', 'qdrant']}
        site = self.site()
        services = render_compose(site, images, 10017, 10017)['services']
        for name in ['api', 'qwen']:
            spec = services[name]
            self.assertEqual(spec['devices'], [cdi_device(site)])
            self.assertNotIn('deploy', spec)
            self.assertEqual(spec['runtime'], 'runc')
            self.assertEqual(spec['environment']['NVIDIA_VISIBLE_DEVICES'], 'void')
            self.assertEqual(spec['user'], '10017:10017')
            self.assertTrue(spec['read_only'])
            self.assertEqual(spec['cap_drop'], ['ALL'])
            self.assertEqual(spec['security_opt'], ['no-new-privileges:true'])
        for name in ['worker', 'init', 'embedding', 'frontend', 'mysql', 'redis', 'qdrant']:
            self.assertNotIn('devices', services[name])
            self.assertEqual(services[name]['environment']['NVIDIA_VISIBLE_DEVICES'], 'void')
            self.assertEqual(services[name]['runtime'], 'runc')

    def test_inspection_accepts_cdi_and_rejects_device_env_runtime_drift(self):
        site = self.site()
        original = self.inspection(site)
        validate_inspection(site, original, True)
        mutations = [
            lambda x: x['HostConfig']['DeviceRequests'][0].update(DeviceIDs=['nvidia.com/gpu=all']),
            lambda x: x['HostConfig']['DeviceRequests'].append(copy.deepcopy(x['HostConfig']['DeviceRequests'][0])),
            lambda x: x['HostConfig'].update(DeviceRequests=[{'Driver': 'nvidia'}]),
            lambda x: x['HostConfig'].update(Runtime='nvidia'),
            lambda x: x['Config'].update(Env=['NVIDIA_VISIBLE_DEVICES=all']),
            lambda x: x['HostConfig'].update(Devices=[{'PathOnHost': '/dev/nvidia0'}]),
            lambda x: x['HostConfig'].update(DeviceCgroupRules=['c 195:* rwm']),
        ]
        for mutate in mutations:
            changed = copy.deepcopy(original)
            mutate(changed)
            with self.assertRaises(DeploymentError):
                validate_inspection(site, changed, True)
        with self.assertRaises(DeploymentError):
            validate_inspection(site, original, False)

    def test_cdi_device_mapping_representation_is_exact(self):
        site = self.site('3')
        row = {'HostConfig': {'Runtime': 'runc', 'DeviceRequests': [], 'Devices': [
            {'PathOnHost': cdi_device(site), 'PathInContainer': '', 'CgroupPermissions': ''}]},
            'Config': {'Env': ['NVIDIA_VISIBLE_DEVICES=void']}}
        validate_inspection(site, row, True)
        row['HostConfig']['Devices'][0]['PathInContainer'] = '/dev/nvidia0'
        with self.assertRaises(DeploymentError):
            validate_inspection(site, row, True)

    def test_probe_checks_uuid_and_runs_nonroot_restricted_cuda(self):
        site = self.site()
        output = json.dumps({'uuid': site['gpu'], 'visible_gpus': 1}) + '\nCUDA_OK\n'
        with patch('ccheck.gpu.run', side_effect=[output, site['gpu']]) as run:
            result = probe(site, 'sha256:' + 'a' * 64, cuda=True, uid=10017, gid=10017)
        self.assertTrue(result['cuda_tested'])
        command = run.call_args_list[0].args[0]
        self.assertIn('10017:10017', command)
        self.assertIn('--read-only', command)
        self.assertIn('no-new-privileges:true', command)
        self.assertIn(cdi_device(site), command)
        with patch('ccheck.gpu.run', side_effect=[output, 'GPU-other']), self.assertRaises(DeploymentError):
            probe(site, 'sha256:' + 'a' * 64)

    def test_legacy_inspection_rejects_extra_devices(self):
        site = self.site('2', 'legacy')
        row = {'HostConfig': {'DeviceRequests': [{'Driver': 'nvidia', 'DeviceIDs': ['2'],
                  'Capabilities': [['gpu']], 'Count': 0, 'Options': {}}]}, 'Config': {}}
        validate_inspection(site, row, True)
        row['HostConfig']['DeviceRequests'][0]['DeviceIDs'] = ['2', '3']
        with self.assertRaises(DeploymentError):
            validate_inspection(site, row, True)

    def test_non_gpu_service_rejects_environment_runtime_backdoor(self):
        row = {'HostConfig': {'Runtime': 'runc'}, 'Config': {'Env': ['NVIDIA_VISIBLE_DEVICES=void']}}
        validate_inspection(self.site(), row, False)
        for runtime, value in [('nvidia', 'void'), ('runc', 'all'), ('nvidia', 'all')]:
            row['HostConfig']['Runtime'] = runtime
            row['Config']['Env'] = ['NVIDIA_VISIBLE_DEVICES=' + value]
            with self.assertRaises(DeploymentError):
                validate_inspection(self.site(), row, False)


if __name__ == '__main__':
    unittest.main()
