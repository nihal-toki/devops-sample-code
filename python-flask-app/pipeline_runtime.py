"""Start, verify and clean up the deployed Flask server from Jenkins stages."""
import argparse
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.request

EXPECTED = 'Hello, Jenkins Multi-Stage Pipeline!'

def stamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def write(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')

def process_start(pid):
    try:
        # Skip the parenthesized command name before indexing Linux stat fields.
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return fields[19] if fields[0] != 'Z' else None
    except FileNotFoundError:
        return None

def command(pid):
    try:
        return Path(f'/proc/{pid}/cmdline').read_bytes().replace(b'\0', b' ').decode()
    except FileNotFoundError:
        return ''

def same_process(state):
    return process_start(state['pid']) == state['start_ticks'] and state['helper'] in command(state['pid']) and ' serve ' in command(state['pid']) and state['deployed'] in command(state['pid'])

def serve(deployed, state_file):
    from werkzeug.serving import make_server
    module_spec = importlib.util.spec_from_file_location('exercise9_deployed_app', deployed)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    server = make_server('127.0.0.1', 0, module.app)
    write(state_file, {'pid': os.getpid(), 'start_ticks': process_start(os.getpid()),
                       'helper': str(Path(__file__).resolve()), 'deployed': str(deployed),
                       'module': str(Path(module.__file__).resolve()), 'port': server.server_port,
                       'started_at': stamp()})
    server.serve_forever()

def stop(deployed, state_file):
    report_file = deployed.parent / 'server-cleanup.json'
    if not state_file.exists():
        write(report_file, {'checked_at': stamp(), 'server_started': False,
                            'process_stopped': True, 'note': 'No runtime state file exists.'})
        return
    state = json.loads(state_file.read_text())
    if process_start(state['pid']) is not None:
        if not same_process(state):
            raise RuntimeError('PID identity changed; refusing to signal another process')
        os.kill(state['pid'], signal.SIGTERM)
        for _ in range(100):
            if process_start(state['pid']) is None:
                break
            time.sleep(.1)
        else:
            if not same_process(state):
                raise RuntimeError('Process identity changed during cleanup')
            os.kill(state['pid'], signal.SIGKILL)
            for _ in range(50):
                if process_start(state['pid']) is None:
                    break
                time.sleep(.1)
    stopped = process_start(state['pid']) is None
    report = {'checked_at': stamp(), 'server_started': True, 'pid': state['pid'],
              'deployed': state['deployed'], 'process_stopped': stopped,
              'identity_checked_before_signal': True}
    write(report_file, report)
    if not stopped:
        raise RuntimeError('Server process did not stop')
    state_file.unlink()
    print('Stopped deployed server PID', state['pid'])

def start(deployed, state_file):
    if state_file.exists():
        stop(deployed, state_file)
    # A distinct process cookie lets this server survive the Jenkins sh step.
    # Explicit post-build cleanup, not Jenkins's end-of-step process sweep, owns it.
    env = {**os.environ, 'JENKINS_NODE_COOKIE': 'exercise9-http-' + os.environ.get('BUILD_TAG', str(os.getpid())),
           'PYTHONDONTWRITEBYTECODE': '1'}
    with (deployed.parent / 'app.log').open('w') as log:
        child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'serve',
                                  '--deployed', str(deployed)], cwd=deployed.parent,
                                 env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 start_new_session=True)
    try:
        for _ in range(100):
            if state_file.exists():
                state = json.loads(state_file.read_text())
                if same_process(state):
                    print('Started deployed server PID', state['pid'], 'on loopback port', state['port'])
                    return
            if child.poll() is not None:
                raise RuntimeError((deployed.parent / 'app.log').read_text())
            time.sleep(.1)
        raise RuntimeError('Server readiness timed out')
    except BaseException:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        raise

def verify(deployed, state_file, source):
    state = json.loads(state_file.read_text())
    identity = same_process(state)
    if not identity:
        raise RuntimeError('Deployed server process is absent or has changed identity')
    url = 'http://127.0.0.1:' + str(state['port']) + '/'
    with urllib.request.urlopen(url, timeout=10) as response:
        status, body = response.status, response.read().decode()
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    deployed_hash = hashlib.sha256(deployed.read_bytes()).hexdigest()
    checks = {'http_200': status == 200, 'exact_response': body == EXPECTED,
              'deployed_module_identity': state['module'] == str(deployed),
              'pid_identity': identity, 'source_and_deployed_hash_match': source_hash == deployed_hash}
    report = {'checked_at': stamp(), 'build_number': os.environ.get('BUILD_NUMBER'),
              'url': url, 'status': status, 'body': body, 'pid': state['pid'], 'start_ticks': state['start_ticks'],
              'command': command(state['pid']), 'module': state['module'],
              'source_sha256': source_hash, 'deployed_sha256': deployed_hash,
              'checks': checks, 'all_passed': all(checks.values()),
              'method': 'Real HTTP request from the Jenkins Test Application stage to the deployed server started in Run Application.'}
    write(deployed.parent / 'http-response.json', report)
    print(json.dumps(report, indent=2))
    if not report['all_passed']:
        raise RuntimeError('Deployed HTTP verification failed')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['start', 'serve', 'verify', 'stop'])
    parser.add_argument('--deployed', type=Path, required=True)
    parser.add_argument('--source', type=Path)
    args = parser.parse_args()
    deployed = args.deployed.resolve()
    state_file = deployed.parent / 'server-state.json'
    if args.action == 'serve':
        serve(deployed, state_file)
    elif args.action == 'start':
        start(deployed, state_file)
    elif args.action == 'stop':
        stop(deployed, state_file)
    else:
        if args.source is None:
            parser.error('--source is required for verify')
        verify(deployed, state_file, args.source.resolve())

if __name__ == '__main__':
    main()
