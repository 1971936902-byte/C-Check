import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'cli'))
from build_frontend_patch import BASE_SHA, validate_sources
from ccheck.core import DeploymentError, load_site
from ccheck.engine import render_compose
from ccheck.terminal_config import validate_file


class FrontendPatchTests(unittest.TestCase):
    def test_update_carries_only_explicit_terminal_path(self):
        from ccheck.__main__ import main
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'config').mkdir()
            (root/'config/site.json').write_text('{}')
            saved = {'instance':'patch-test','data_dir':folder,'web':{'bind':'127.0.0.1','port':18800}}
            external = copy.deepcopy(saved)
            external['web'].update(terminal_config='/etc/c-check/terminal.json',port=19999)
            with patch.object(sys,'argv',['ccheck','update','--config',str(root/'external.json'),
                                         '--backup-key',str(root/'backup.key')]), \
                 patch('ccheck.__main__.load_site',side_effect=[external,saved]), \
                 patch('ccheck.__main__.lifecycle.update') as update:
                self.assertEqual(main(),0)
                self.assertEqual(update.call_args.args[0]['web'],
                    {'bind':'127.0.0.1','port':18800,'terminal_config':'/etc/c-check/terminal.json'})

    def test_rejects_a3b_and_backend_changes(self):
        old = {'git_sha':'a'*40,'tree_sha256':'b'*64,'files':{'backend/app/main.py':'original'}}
        base = {'version':'v0.1.0-rc.19','_sha256':BASE_SHA,'source':old}
        new = copy.deepcopy(old)
        new['files']['frontend/src/views/HelpView.vue'] = 'new'
        self.assertEqual(validate_sources(base,new,old), ['frontend/src/views/HelpView.vue'])
        for path in ('backend/app/main.py','deploy/models/manifest.json','frontend/package-lock.json',
                     'deploy/release/cli/ccheck/launchers/qwen_gpu.py'):
            changed = copy.deepcopy(new)
            changed['files'][path] = 'forbidden'
            with self.assertRaises(RuntimeError):
                validate_sources(base,changed,old)
        base['version'] = 'v0.1.0-rc.17'
        with self.assertRaises(RuntimeError):
            validate_sources(base,new,old)

    def test_optional_mount_only_changes_frontend(self):
        site = {'instance':'patch-test','data_dir':'/srv/c-check','gpu':'0','gpu_access_mode':'cdi',
                'profile':'ada-48g-fp8','web':{'bind':'127.0.0.1','port':18800,'public_url':'http://127.0.0.1:18800'}}
        images = {n:'sha256:'+'a'*64 for n in ('backend','frontend','qwen','embedding','mysql','redis','qdrant')}
        before = render_compose(site,images,10001,10001)
        site['web']['terminal_config'] = '/srv/terminal-link.json'
        after = render_compose(site,images,10001,10001)
        volume = after['services']['frontend'].pop('volumes')
        self.assertEqual(before,after)
        self.assertEqual(volume,[{'type':'bind','source':'/srv/terminal-link.json',
            'target':'/usr/share/nginx/html/terminal-config.json','read_only':True,'bind':{'create_host_path':False}}])

    def test_config_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'terminal.json'
            for url in ('','https://terminal.example.com/guacamole/','https://localhost:18843/guacamole/'):
                path.write_text(json.dumps({'url':url}))
                self.assertEqual(validate_file(str(path)), {'url':url})
            for data in ({'url':'http://example.com'}, {'url':'https://user:pass@example.com'},
                         {'url':'https://example.com/?token=secret'}, {'url':'https://example.com/#'},
                         {'url':'https://example.com\\@evil.test'}, {'url':' https://example.com'},
                         {'url':'https://example.com:99999'}, {'url':None}, {'url':'','password':'secret'}, []):
                path.write_text(json.dumps(data))
                with self.assertRaises(DeploymentError):
                    validate_file(str(path))
            path.write_text('{"url":"","url":"https://example.com"}')
            with self.assertRaises(DeploymentError):
                validate_file(str(path))
            path.write_text('x'*4097)
            with self.assertRaises(DeploymentError):
                validate_file(str(path))
        with self.assertRaises(DeploymentError):
            validate_file('relative.json')


if __name__ == '__main__':
    unittest.main()
