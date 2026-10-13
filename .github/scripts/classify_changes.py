"""Skip build/test jobs only for known documentation changes; otherwise run CI."""
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess

DOC_SUFFIXES = {'.md', '.rst', '.txt', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.pdf'}


def is_documentation(path):
    parts = PurePosixPath(path).parts
    if not parts or '..' in parts or path.startswith('/'):
        return False
    name = parts[-1]
    suffix = PurePosixPath(name).suffix.lower()
    if len(parts) == 1:
        return suffix in {'.md', '.rst'} or name in {'LICENSE', 'NOTICE', 'COPYING', 'AUTHORS'}
    if parts[0] == 'docs':
        return suffix in DOC_SUFFIXES
    return parts[0] == 'scripts' and name.startswith('README') and suffix in {'.md', '.rst'}


def requires_ci(paths):
    return not paths or not all(is_documentation(path) for path in paths)


def git(*args, cwd=None):
    return subprocess.check_output(['git', *args], cwd=cwd, stderr=subprocess.PIPE)


def classify(event_name, event, cwd=None):
    if event_name == 'workflow_dispatch':
        return True, 'manual dispatch always runs full CI', []
    if event_name == 'pull_request':
        request = event.get('pull_request', {})
        before = request.get('base', {}).get('sha', '')
        after = request.get('head', {}).get('sha', '')
    elif event_name == 'push':
        before, after = event.get('before', ''), event.get('after', '')
    else:
        return True, 'unrecognized event runs full CI', []
    if any(not re.fullmatch(r'[0-9a-f]{40}', sha) or sha == '0' * 40 for sha in (before, after)):
        return True, 'missing comparison runs full CI', []
    try:
        if event_name == 'pull_request':
            before = git('merge-base', before, after, cwd=cwd).decode().strip()
        # A source-to-document rename must include the deleted source path.
        paths = [os.fsdecode(path) for path in git('diff', '--no-renames', '--name-only', '-z',
                                                  before, after, cwd=cwd).split(b'\0') if path]
    except (OSError, subprocess.CalledProcessError):
        return True, 'comparison unavailable runs full CI', []
    full = requires_ci(paths)
    return full, 'source/configuration or empty change' if full else 'documentation only', paths


def main():
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
    full, reason, paths = classify(os.environ['GITHUB_EVENT_NAME'], event)
    with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as output:
        output.write('run_ci=' + str(full).lower() + '\n')
    print(json.dumps({'run_ci': full, 'reason': reason, 'paths': paths}, ensure_ascii=True))


if __name__ == '__main__':
    main()
