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
    if not gpu_service:
        if requests or devices:
            raise DeploymentError('unexpected GPU/device grant on a non-GPU service')
        return
    if access_mode(site) == 'cdi':
        expected = cdi_device(site)
        if requests or len(devices) != 1 or devices[0].get('PathOnHost') != expected:
            raise DeploymentError('CDI device grant differs from the selected single GPU')
        if devices[0].get('PathInContainer') not in {'', expected} or devices[0].get('CgroupPermissions') not in {'', 'rwm'}:
            raise DeploymentError('unexpected CDI device mapping')
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


NVML_PROBE = (
    "import ctypes,json; n=ctypes.CDLL('libnvidia-ml.so.1');"
    "assert n.nvmlInit_v2()==0; count=ctypes.c_uint();"
    "assert n.nvmlDeviceGetCount_v2(ctypes.byref(count))==0 and count.value==1;"
    "h=ctypes.c_void_p(); assert n.nvmlDeviceGetHandleByIndex_v2(0,ctypes.byref(h))==0;"
    "u=ctypes.create_string_buffer(96); assert n.nvmlDeviceGetUUID(h,u,len(u))==0;"
    "print(json.dumps({'uuid':u.value.decode(),'visible_gpus':count.value}),flush=True);"
    "assert n.nvmlShutdown()==0;"
)
CUDA_PROBE = NVML_PROBE + (
    "import torch,vllm,transformers; assert torch.cuda.is_available() and torch.cuda.device_count()==1;"
    "assert torch.__version__.split('+')[0]=='2.6.0' and torch.version.cuda=='12.4';"
    "assert vllm.__version__=='0.8.5.post1' and transformers.__version__=='4.51.3';"
    "x=torch.ones(1,device='cuda'); assert x.item()==1; torch.cuda.synchronize();"
    "print('CUDA_OK',torch.__version__,torch.cuda.get_device_name(0),flush=True)"
)


def probe(site, image, *, cuda=False, uid=10001, gid=10001):
    output = run(['docker', 'run', '--rm', *docker_arguments(site), '--network', 'none',
                  '--user', f'{uid}:{gid}', '--read-only', '--cap-drop', 'ALL',
                  '--security-opt', 'no-new-privileges:true', '--tmpfs', '/tmp:rw,exec,mode=1777',
                  '-e', 'HOME=/tmp', '--entrypoint', 'python', image, '-c',
                  CUDA_PROBE if cuda else NVML_PROBE], timeout=180)
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
