import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cli'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ccheck import delivery
from ccheck.core import DeploymentError
from ccheck.models import allow_model_download, model_cache_file, obtain_file
from build_online_release import online_images, verify_inputs


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.site = {'data_dir': str(self.root/'instance'), 'delivery': {'cache_dir': str(self.root/'downloads')}}
        self.spec = {'id': 'sha256:'+'a'*64, 'filesystem_config_sha256': 'b'*64,
            'remote_parts': [{'name': 'app.tar.gz.part0000', 'url': 'https://example.com/app.tar.gz.part0000',
                              'sha256': 'c'*64, 'size': 123}]}
        self.release = {'version': 'v0.2.0-rc.1', 'distribution': {'kind': 'online'},
                        'runtime_recipe': None, 'images': {'backend': self.spec}}

    def test_source_registry_digest_and_mirror_preserve_identity(self):
        ref='mysql@sha256:'+'a'*64
        self.assertEqual(delivery.registry_reference(ref, {}), 'docker.io/library/'+ref)
        self.assertEqual(delivery.registry_reference(ref, {'docker.io':'harbor.example/cache'}),
                         'harbor.example/cache/library/'+ref)
        for bad in ['mysql:8', 'mysql:latest', 'https://docker.io/mysql@sha256:'+'a'*64, '-evil@sha256:'+'a'*64]:
            with self.assertRaises(DeploymentError):
                delivery.registry_reference(bad,{})

    def test_configuration_rejects_credentials_unknown_fields_and_non_https(self):
        for value in [{'offline':'false'}, {'cache_dir':'relative'}, {'unexpected':1},
                      {'artifact_base_url':'http://example.com'}, {'artifact_base_url':'https://u:p@example.com'},
                      {'registry_mirrors':{'docker.io':'https://evil.example'}}]:
            self.site['delivery']=dict({'cache_dir':str(self.root/'downloads')},**value)
            with self.subTest(value=value), self.assertRaises(DeploymentError):
                delivery.settings(self.site)

    def test_signed_online_recipe_refuses_bundled_parts_mutable_tags_and_bad_hashes(self):
        delivery.validate_release(self.release)
        for key,value in [('archive_parts',['images/a']),('reference','mysql:latest'),
                          ('remote_parts',[dict(self.spec['remote_parts'][0],name='../bad')])]:
            release=copy.deepcopy(self.release)
            release['images']['backend'][key]=value
            with self.assertRaises(DeploymentError):
                delivery.validate_release(release)

    def test_remote_parts_are_cached_outside_release_and_mirror_cannot_change_hash(self):
        self.site['delivery']['artifact_base_url']='https://mirror.example/releases'
        with patch('ccheck.models.obtain_file') as obtain:
            paths=delivery.cache_image_parts(self.spec,self.site)
        entry=obtain.call_args.args[0]
        self.assertEqual(entry['url'],'https://mirror.example/releases/app.tar.gz.part0000')
        self.assertEqual(entry['sha256'],'c'*64)
        self.assertTrue(paths[0].is_relative_to(self.root/'downloads'))
        self.site['delivery']['offline']=True
        with patch('ccheck.models.obtain_file') as obtain:
            delivery.cache_image_parts(self.spec,self.site)
            self.assertFalse(obtain.call_args.kwargs['allow_download'])

    def test_pinned_registry_pull_does_not_use_archive_when_identity_matches(self):
        self.spec['reference']='redis@sha256:'+'a'*64
        with patch('ccheck.delivery.run') as run, patch('ccheck.image_identity.locate',return_value=self.spec['id']), \
                patch('ccheck.delivery.cache_image_parts') as parts:
            self.assertEqual(delivery.import_image(self.spec,self.site),'registry')
        self.assertIn('--platform',run.call_args.args[0])
        parts.assert_not_called()

    def test_global_offline_also_disables_model_network_access(self):
        self.site['delivery']['offline']=True
        self.assertFalse(allow_model_download(self.site))
        self.site['delivery']['offline']=False
        self.site['model_download_policy']='offline'
        self.assertFalse(allow_model_download(self.site))

    def test_model_prefetch_is_reused_without_writing_explicit_model_directory(self):
        model={'key':'qwen','cache_name':'qwen-fixed'}
        entry={'path':'config.json','size':2}
        self.site['model_paths']={'qwen':str(self.root/'read-only-source')}
        cached=delivery.model_file(self.site,model,entry)
        cached.parent.mkdir(parents=True)
        cached.write_bytes(b'{}')
        self.assertEqual(model_cache_file(self.site,model,entry),cached)
        self.assertFalse((self.root/'read-only-source').exists())

    @unittest.skipIf(os.name!='posix','POSIX cache locks')
    def test_cache_lock_rejects_world_access_and_parallel_preparation(self):
        path=Path(self.site['delivery']['cache_dir'])
        with delivery.cache_lock(self.site):
            with self.assertRaises(DeploymentError):
                with delivery.cache_lock(self.site):
                    pass
        path.chmod(0o755)
        with self.assertRaises(DeploymentError):
            with delivery.cache_lock(self.site):
                pass

    def test_online_plan_is_read_only_and_reports_download_budget(self):
        with patch('ccheck.image_identity.locate',return_value=None):
            result=delivery.plan(self.release,self.site)
        self.assertEqual(result['images'][0]['archive_download_bytes_if_needed'],123)
        self.assertFalse(Path(result['cache_dir']).exists())

    def test_online_conversion_preserves_images_and_resolves_original_archive_release(self):
        base={'version':'v0.1.0-rc.16','distribution':{'kind':'deployment-patch','base_version':'v0.1.0-rc.14',
              'reused_files':['images/app.tar.gz.part0000']},
              'files':{'images/app.tar.gz.part0000':'c'*64},'artifact_sizes':{'images/app.tar.gz.part0000':123},
              'images':{'backend':dict(self.spec,archive_parts=['images/app.tar.gz.part0000'])}}
        images=online_images(base,'https://github.com/o/r/releases/download')
        self.assertEqual(images['backend']['remote_parts'][0]['url'],
                         'https://github.com/o/r/releases/download/release-v0.1.0-rc.14/app.tar.gz.part0000')
        self.assertNotIn('archive_parts',images['backend'])
        self.assertIn('archive_parts',base['images']['backend'])

    def test_resumed_artifact_requires_correct_range_size_and_hash(self):
        target=self.root/'resumed.bin'
        target.with_name('resumed.bin.part').write_bytes(b'a')
        entry={'size':3,'sha256':hashlib.sha256(b'abc').hexdigest(),'url':'https://example.com/fixed'}
        response=io.BytesIO(b'bc')
        response.url=entry['url']
        response.status=206
        response.headers={'Content-Range':'bytes 1-2/3'}
        with patch('ccheck.models.urlopen',return_value=response) as download, \
                patch('ccheck.models.shutil.disk_usage',return_value=SimpleNamespace(free=10*1024**3)):
            self.assertEqual(obtain_file(entry,target),'downloaded')
        self.assertEqual(download.call_args.args[0].get_header('Range'),'bytes=1-')
        self.assertEqual(target.read_bytes(),b'abc')

    def test_corrupt_download_never_becomes_a_verified_cache_entry(self):
        target=self.root/'bad.bin'
        entry={'size':3,'sha256':hashlib.sha256(b'abc').hexdigest(),'url':'https://example.com/fixed'}
        def response(*args,**kwargs):
            stream=io.BytesIO(b'bad')
            stream.url=entry['url']
            stream.status=200
            return stream
        with patch('ccheck.models.urlopen',side_effect=response) as download, \
                patch('ccheck.models.time.sleep'), \
                patch('ccheck.models.shutil.disk_usage',return_value=SimpleNamespace(free=10*1024**3)):
            with self.assertRaises(DeploymentError):
                obtain_file(entry,target)
        self.assertEqual(download.call_count,4)
        self.assertFalse(target.exists())

    def test_online_builder_cannot_smuggle_application_changes(self):
        previous={'git_sha':'a'*40,'tree_sha256':'b'*64,'files':{'backend/app/main.py':'old'}}
        base={'source':{key:previous[key] for key in ['git_sha','tree_sha256']}}
        with patch('build_online_release.verify_source',return_value=previous):
            with self.assertRaises(RuntimeError):
                verify_inputs(base,{'files':{'backend/app/main.py':'changed'}},self.root)
            changed=verify_inputs(base,{'files':dict(previous['files'],**{
                'deploy/release/build_online_release.py':'new'})},self.root)
        self.assertEqual(changed,['deploy/release/build_online_release.py'])

    def test_prefetch_refuses_image_expansion_when_shared_disk_budget_is_insufficient(self):
        cache=Path(self.site['delivery']['cache_dir'])
        cache.mkdir()
        self.spec['size_bytes']=4*1024**3
        with patch('ccheck.image_identity.locate',return_value=None), \
                patch('ccheck.delivery.run',return_value=str(self.root)), \
                patch('shutil.disk_usage',return_value=SimpleNamespace(free=3*1024**3)):
            with self.assertRaisesRegex(DeploymentError,'no services were stopped'):
                delivery.check_space(self.release,self.site)


if __name__=='__main__':
    unittest.main()
