"""Explicit, per-container GPU access; never changes the shared Docker daemon."""
import json
import re

from .core import DeploymentError, run


def access_mode(site):
    mode = site.get('gpu_access_mode', 'legacy')
    if mode not in {'legacy', 'cdi'}:
        raise DeploymentError('gpu_access_mode must be legacy or cdi')
    return mode


def cdi_device(site):
    gpu = str(site['gpu'])
    if not re.fullmatch(r'\d+|GPU-[a-fA-F0-9-]+', gpu):
        raise DeploymentError('select exactly one GPU index or UUID, never all')
    return 'nvidia.com/gpu=' + gpu


def docker_arguments(site):
    if access_mode(site) == 'cdi':
        return ['--runtime', 'runc', '--device', cdi_device(site), '-e', 'NVIDIA_VISIBLE_DEVICES=void']
    return ['--gpus', 'device=' + str(site['gpu'])]


def configure(service, site):
    if access_mode(site) == 'cdi':
        service['devices'] = [cdi_device(site)]
        service['runtime'] = 'runc'
        service['environment'] = dict(service.get('environment', {}), NVIDIA_VISIBLE_DEVICES='void')
    else:
        service['deploy'] = {'resources': {'reservations': {'devices': [
            {'driver': 'nvidia', 'device_ids': [str(site['gpu'])], 'capabilities': ['gpu']}]}}}


def validate_inspection(site, inspection, gpu_service):
    host = inspection['HostConfig']
    requests = host.get('DeviceRequests') or []
    devices = host.get('Devices') or []
    runtime = host.get('Runtime') or 'runc'
    if host.get('DeviceCgroupRules'):
        raise DeploymentError('unexpected extra device cgroup permissions')
    if not gpu_service:
        if requests or devices:
            raise DeploymentError('unexpected GPU/device grant on a non-GPU service')
        environment = dict(item.split('=', 1) for item in inspection['Config'].get('Env', []) if '=' in item)
        if runtime not in {'runc', 'io.containerd.runc.v2'} or environment.get('NVIDIA_VISIBLE_DEVICES') != 'void':
            raise DeploymentError('non-GPU service must disable NVIDIA runtime/environment injection')
        return
    if access_mode(site) == 'cdi':
        expected = cdi_device(site)
        native_request = (not devices and len(requests) == 1 and requests[0].get('Driver') == 'cdi'
                          and requests[0].get('DeviceIDs') == [expected] and not requests[0].get('Count')
                          and not requests[0].get('Capabilities') and not requests[0].get('Options'))
        device_mapping = (not requests and len(devices) == 1 and devices[0].get('PathOnHost') == expected
                          and devices[0].get('PathInContainer') in {'', expected}
                          and devices[0].get('CgroupPermissions') in {'', 'rwm'})
        if not (native_request or device_mapping):
            raise DeploymentError('CDI device grant differs from the selected single GPU')
        if runtime not in {'runc', 'io.containerd.runc.v2'}:
            raise DeploymentError('native CDI requires the standard runc runtime')
        environment = dict(item.split('=', 1) for item in inspection['Config'].get('Env', []) if '=' in item)
        if environment.get('NVIDIA_VISIBLE_DEVICES') != 'void':
            raise DeploymentError('native CDI must disable legacy NVIDIA environment injection')
    elif (devices or len(requests) != 1 or requests[0].get('Driver') != 'nvidia'
          or requests[0].get('DeviceIDs') != [str(site['gpu'])]
          or requests[0].get('Capabilities') != [['gpu']]
          or requests[0].get('Count', 0) != 0 or requests[0].get('Options')):
        raise DeploymentError('legacy GPU grant differs from the selected single GPU')


NVML_PROBE = '''import ctypes,json
n=ctypes.CDLL('libnvidia-ml.so.1')
assert n.nvmlInit_v2()==0
count=ctypes.c_uint()
assert n.nvmlDeviceGetCount_v2(ctypes.byref(count))==0
accessible=[]
for index in range(count.value):
    h=ctypes.c_void_p()
    status=n.nvmlDeviceGetHandleByIndex_v2(index,ctypes.byref(h))
    if status==4:
        continue
    assert status==0, ('nvml handle',index,status)
    u=ctypes.create_string_buffer(96)
    status=n.nvmlDeviceGetUUID(h,u,len(u))
    if status==4:
        continue
    assert status==0, ('nvml uuid',index,status)
    accessible.append(u.value.decode())
assert len(accessible)==1, accessible
print(json.dumps({'uuid':accessible[0],'visible_gpus':len(accessible)}),flush=True)
assert n.nvmlShutdown()==0
'''
CUDA_PROBE = NVML_PROBE + (
    "import torch,vllm,transformers; assert torch.cuda.is_available() and torch.cuda.device_count()==1;"
    "assert torch.__version__.split('+')[0]=='2.6.0' and torch.version.cuda=='12.4';"
    "assert vllm.__version__=='0.8.5.post1' and transformers.__version__=='4.51.3';"
    "x=torch.ones(1,device='cuda'); assert x.item()==1; torch.cuda.synchronize();"
    "print('CUDA_OK',torch.__version__,torch.cuda.get_device_name(0),flush=True)"
)

QWEN3_CUDA_PROBE = NVML_PROBE + (
    "import torch,vllm,transformers; assert torch.cuda.is_available() and torch.cuda.device_count()==1;"
    "assert torch.__version__.split('+')[0]=='2.8.0' and torch.version.cuda=='12.8';"
    "assert vllm.__version__=='0.10.2' and transformers.__version__=='4.55.2';"
    "x=torch.ones(1,device='cuda'); assert x.item()==1; torch.cuda.synchronize();"
    "print('CUDA_OK',torch.__version__,torch.cuda.get_device_name(0),flush=True)"
)


def cuda_probe_program(site):
    from .inference import QWEN3, served_name
    return QWEN3_CUDA_PROBE if served_name(site) == QWEN3 else CUDA_PROBE


def probe(site, image, *, cuda=False, uid=10001, gid=10001):
    output = run(['docker', 'run', '--rm', *docker_arguments(site), '--network', 'none',
                  '--user', f'{uid}:{gid}', '--read-only', '--cap-drop', 'ALL',
                  '--security-opt', 'no-new-privileges:true', '--tmpfs', '/tmp:rw,exec,mode=1777',
                  '-e', 'HOME=/tmp', '--entrypoint', 'python', image, '-c',
                  cuda_probe_program(site) if cuda else NVML_PROBE], timeout=180)
    return check_identity(site, output, cuda=cuda)


def check_identity(site, output, *, cuda=False):
    expected = run(['nvidia-smi', '-i', str(site['gpu']), '--query-gpu=uuid', '--format=csv,noheader']).strip()
    try:
        result = json.loads(next(line for line in output.splitlines() if line.startswith('{')))
    except (ValueError, StopIteration) as exc:
        raise DeploymentError('GPU probe returned no device identity') from exc
    if result.get('uuid') != expected or result.get('visible_gpus') != 1:
        raise DeploymentError('container GPU identity/visibility differs from the selected GPU')
    return dict(result, access_mode=access_mode(site), cuda_tested=cuda)
