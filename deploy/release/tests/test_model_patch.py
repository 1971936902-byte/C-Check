import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_model_patch as model_patch
from ccheck.core import digest
from ccheck.image_identity import identity


class ModelPatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_only_management_runtime_and_tools_can_change(self):
        files = {'backend/app/services/prompts.py': 'core', 'backend/app/deployment.py': 'old',
                 'deploy/release/Dockerfile.qwen': 'unchanged'}
        previous = {'git_sha': 'a'*40, 'tree_sha256': 'b'*64, 'files': files}
        base = {'source': {'git_sha': 'a'*40, 'tree_sha256': 'b'*64}}
        source = {'files': dict(files, **{'backend/app/deployment.py': 'new',
                                         'deploy/release/cli/ccheck/inference.py': 'tool'})}
        with patch('build_model_patch.verify_source', return_value=previous):
            self.assertEqual(len(model_patch.verify_inputs(base, source, self.root)), 2)
            for forbidden in ('backend/app/services/prompts.py', 'deploy/release/Dockerfile.qwen'):
                candidate = copy.deepcopy(source)
                candidate['files'][forbidden] = 'different'
                with self.assertRaisesRegex(RuntimeError, 'cannot change'):
                    model_patch.verify_inputs(base, candidate, self.root)

    def report_fixture(self):
        manifest = self.root/'manifest.json'
        manifest.write_text('{}')
        (self.root/'locks').mkdir()
        (self.root/'locks/qwen.lock').write_text('vllm==0.10.2\n')
        metadata = {'Os':'linux', 'Architecture':'amd64', 'RootFS':{'Type':'layers','Layers':[]}, 'Config':{}}
        source = {'tree_sha256':'a'*64}
        report = {'passed':True, 'source_tree':source['tree_sha256'],
                  'backend':{'passed':True, 'image_identity':identity(metadata), 'compiled_core_unchanged':True},
                  'runtime':{'passed':True, 'image_identity':identity(metadata),
                             'lock_sha256':digest(self.root/'locks/qwen.lock')},
                  'models':{'passed':True, 'manifest_sha256':digest(manifest)},
                  'tool_tests':{'passed':True}, 'security':{'passed':True}}
        return manifest, metadata, source, report

    def test_all_build_evidence_must_bind_exact_source_model_lock_and_images(self):
        manifest, metadata, source, report = self.report_fixture()
        path = self.root/'report.json'
        with patch('build_model_patch.inspect_image', return_value=metadata):
            path.write_text(json.dumps(report))
            model_patch.verify_report(path, source, manifest, {'backend':'b','qwen':'q'}, self.root)
            mutators = [lambda r:r.update(source_tree='different'),
                        lambda r:r['runtime'].update(lock_sha256='different'),
                        lambda r:r['runtime'].update(image_identity='different'),
                        lambda r:r['models'].update(manifest_sha256='different'),
                        lambda r:r['backend'].update(compiled_core_unchanged=False)]
            mutators += [lambda r, gate=gate:r[gate].update(passed=False)
                         for gate in ('backend','runtime','models','tool_tests','security')]
            for mutate in mutators:
                candidate = copy.deepcopy(report)
                mutate(candidate)
                path.write_text(json.dumps(candidate))
                with self.assertRaises(RuntimeError):
                    model_patch.verify_report(path, source, manifest, {'backend':'b','qwen':'q'}, self.root)

    def test_jina_change_is_rejected_before_loading_new_weights(self):
        (self.root/'models').mkdir()
        previous = {'models':[{'key':'qwen','repository':'old'},
                              {'key':'jina','repository':'jina','revision':'original'}]}
        (self.root/'models/model-manifest.json').write_text(json.dumps(previous))
        candidate = copy.deepcopy(previous)
        candidate['models'][1]['revision'] = 'changed'
        path = self.root/'new-manifest.json'
        path.write_text(json.dumps(candidate))
        with self.assertRaisesRegex(RuntimeError, 'preserve.*Jina'):
            model_patch.verify_models({'_root':str(self.root)}, path, self.root/'no-weights')


if __name__ == '__main__':
    unittest.main()
