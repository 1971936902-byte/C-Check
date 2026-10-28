"""Validate an optional, operator-owned public terminal link configuration."""
import json
from pathlib import Path
from urllib.parse import urlsplit

from .core import DeploymentError


def validate_url(url):
    if not isinstance(url, str):
        raise ValueError('expected a url string')
    if not url:
        return url
    parsed = urlsplit(url)
    if (url != url.strip() or any(c.isspace() or ord(c) < 32 for c in url)
            or '\\' in url or parsed.scheme != 'https' or not parsed.hostname
            or parsed.username is not None or parsed.password is not None or '?' in url or '#' in url):
        raise ValueError('expected an HTTPS URL without credentials, query or fragment')
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError('invalid port')
    return url


def validate_file(value):
    if not isinstance(value, str) or not value or any(c in value for c in '\x00\r\n,'):
        raise DeploymentError('web.terminal_config must be an absolute JSON file path')
    path = Path(value)
    if not path.is_absolute() or any(p.is_symlink() for p in [path, *path.parents]):
        raise DeploymentError('terminal config must be absolute and not symlinked')
    try:
        if not path.is_file() or path.stat().st_size > 4096:
            raise ValueError('not a small regular file')
        def unique(pairs):
            result = {}
            for key, val in pairs:
                if key in result:
                    raise ValueError('duplicate key')
                result[key] = val
            return result
        data = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique)
        if not isinstance(data, dict) or set(data) != {'url'} or not isinstance(data['url'], str):
            raise ValueError('expected only a url string')
        validate_url(data['url'])
        return data
    except (OSError, ValueError, UnicodeError) as exc:
        raise DeploymentError('invalid public terminal config: ' + str(exc)) from exc
