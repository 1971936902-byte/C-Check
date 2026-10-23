"""Signed model selection and separate, immutable Qwen revision directories."""
from pathlib import Path
import re

from .core import DeploymentError, checked_path

LEGACY = 'qwen2.5-coder-32b-instruct-fp8'
QWEN3 = 'qwen3-coder-30b-a3b-instruct-fp8'
NAMES = {LEGACY, QWEN3}


def validate_site(site):
    name = site.get('review_model', LEGACY)
    if not isinstance(name, str) or name not in NAMES:
        raise DeploymentError('unsupported review model')
    revision = site.get('qwen_model_revision')
    if revision is not None and (not isinstance(revision, str) or not re.fullmatch('[0-9a-f]{64}', revision)):
        raise DeploymentError('Qwen model storage revision must be a SHA256')


def served_name(site):
    validate_site(site)
    return site.get('review_model', LEGACY)


def configure(site, release, *, transition=False):
    name = release.get('inference', {}).get('served_model', LEGACY)
    if name not in NAMES:
        raise DeploymentError('signed release declares an unsupported review model')
    revision = release['files']['models/model-manifest.json'] if name == QWEN3 else None
    if not transition:
        if site.get('review_model', name) != name:
            raise DeploymentError('site review model differs from signed release')
        if site.get('qwen_model_revision', revision) != revision:
            raise DeploymentError('site model storage revision differs from signed release')
    if name == QWEN3:
        if site.get('runtime_source') == 'build':
            raise DeploymentError('Qwen3 release requires the validated pinned runtime image')
        site.update(review_model=name, qwen_model_revision=revision)
    else:
        site.pop('review_model', None)
        site.pop('qwen_model_revision', None)
    validate_site(site)


def directory(site, key):
    validate_site(site)
    if key not in {'qwen', 'jina'}:
        raise DeploymentError('unknown model storage key')
    suffix = ('qwen-revisions/' + site['qwen_model_revision']
              if key == 'qwen' and site.get('qwen_model_revision') else key)
    return checked_path(Path(site['data_dir']), 'models/' + suffix)
