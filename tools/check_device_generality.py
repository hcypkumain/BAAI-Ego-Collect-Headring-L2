"""Explicit-scope, path-first governance lint. Never a whole-repository audit."""
import argparse
import ast
import hashlib
import ipaddress
import os
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host.common.strict_json import canonical_json, exact_fields, require, strict_json_loads, utc_timestamp

_RULES = frozenset(('DEVICE_COMPARE', 'DEVICE_TABLE', 'DEVICE_DEFAULT', 'RUNTIME_IP'))
_EXCLUDED = frozenset(('state', 'runtime', 'private', 'secrets', 'keys', 'images',
                       'image', 'data', 'raw', 'logs', 'venv', '.git', '__pycache__'))
_DEVICE = re.compile(r'cp_[0-9]{4}\Z')


def _relative(value):
    require(type(value) is str and value and '\\' not in value and
            not value.startswith('/') and str(PurePosixPath(value)) == value and
            '..' not in PurePosixPath(value).parts and not any(c in value for c in '*?[]\x00'),
            'Explicit canonical relative file required')
    return PurePosixPath(value)


def _excluded(parts):
    return any(part.lower() in _EXCLUDED or part.lower().startswith(('venv-', 'integrations-'))
               or part.lower() == 'integrations' for part in parts)


def _enumerate(root, scope, now_utc):
    """Metadata-only phase. No file contents opened until the whole scope is checked."""
    exact_fields(scope, ('schema_version', 'files', 'exceptions', 'exception_sources', 'exception_reviews'))
    require(type(scope['schema_version']) is int and scope['schema_version'] == 1, 'Invalid scope')
    require(type(scope['files']) is list and len(scope['files']) <= 128 and
            type(scope['exceptions']) is list, 'Invalid scope arrays')
    chosen, excluded, seen = [], [], set()
    for entry in scope['files']:
        exact_fields(entry, ('path', 'category', 'reason'))
        relative = _relative(entry['path'])
        require(entry['path'] not in seen and entry['category'] in
                ('runtime', 'fixture', 'config', 'docs', 'legacy'), 'Invalid scope entry')
        require(type(entry['reason']) is str and entry['reason'].strip(), 'Scope reason required')
        seen.add(entry['path'])
        if _excluded(relative.parts):
            excluded.append({'path': entry['path'], 'rule': 'PATH_EXCLUDED', 'line': 0})
            continue
        target = root
        blocked = False
        for part in relative.parts:
            target = target / part
            try:
                mode = target.lstat().st_mode
            except FileNotFoundError:
                excluded.append({'path': entry['path'], 'rule': 'NOT_INTEGRATED', 'line': 0})
                blocked = True
                break
            if stat.S_ISLNK(mode):
                excluded.append({'path': entry['path'], 'rule': 'SYMLINK_EXCLUDED', 'line': 0})
                blocked = True
                break
        if not blocked:
            require(stat.S_ISREG(mode) and relative.suffix in ('.py', '.json', '.md'), 'Not source text')
            chosen.append(entry)
    require(type(scope['exception_sources']) is dict and all(
        path in seen and type(sha) is str and re.fullmatch('[0-9a-f]{64}', sha)
        for path, sha in scope['exception_sources'].items()), 'Invalid exception source bindings')
    require(type(scope['exception_reviews']) is dict and
            set(scope['exception_reviews']) == set(scope['exception_sources']), 'Missing exception review')
    for review in scope['exception_reviews'].values():
        exact_fields(review, ('owner', 'reason', 'reviewed_at', 'expiry'))
        require(all(type(review[key]) is str and bool(review[key].strip()) for key in ('owner', 'reason')),
                'Review owner/reason required')
        reviewed, expiry = utc_timestamp(review['reviewed_at']), utc_timestamp(review['expiry'])
        require(reviewed <= now_utc and reviewed < expiry, 'Invalid review interval')
    exceptions = {}
    for entry in scope['exceptions']:
        exact_fields(entry, ('path', 'rule', 'symbol', 'category', 'reason'))
        _relative(entry['path'])
        require(entry['path'] in seen and entry['rule'] in _RULES and
                entry['path'] in scope['exception_sources'] and
                type(entry['symbol']) is str and bool(entry['symbol']) and
                entry['category'] in ('fixture', 'config', 'historical_scope', 'legacy_debt') and
                type(entry['reason']) is str and bool(entry['reason'].strip()), 'Invalid exception')
        key = (entry['path'], entry['rule'], entry['symbol'])
        require(key not in exceptions, 'Duplicate exception')
        review = scope['exception_reviews'][entry['path']]
        exceptions[key] = {**entry, 'sha256': scope['exception_sources'][entry['path']],
                           'review': review, 'valid': utc_timestamp(review['expiry']) > now_utc}
    return chosen, excluded, exceptions


def _read_source(root, relative):
    # Recheck with O_NOFOLLOW at every path component to close enumeration/read symlink races.
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = PurePosixPath(relative).parts
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        source = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            require(stat.S_ISREG(os.fstat(source).st_mode), 'Not a regular source')
            data = bytearray()
            while len(data) <= 1048576:
                block = os.read(source, min(65536, 1048577 - len(data)))
                if not block:
                    break
                data.extend(block)
            require(len(data) <= 1048576, 'Source size exceeded')
            return data.decode('utf-8')
        finally:
            os.close(source)
    finally:
        os.close(fd)


def _device_literals(node):
    return any(isinstance(child, ast.Constant) and type(child.value) is str and
               _DEVICE.fullmatch(child.value) for child in ast.walk(node))


def _findings(text):
    tree = ast.parse(text)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}

    def symbol(node):
        while node in parents:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                return node.name
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                target = node.targets[0] if isinstance(node, ast.Assign) else node.target
                if isinstance(target, ast.Name):
                    return target.id
            node = parents[node]
        return '<module>'

    found = set()
    for node in ast.walk(tree):
        rule = None
        if isinstance(node, ast.Compare) and _device_literals(node):
            rule = 'DEVICE_COMPARE'
        elif isinstance(node, ast.Call) and any(
                kw.arg == 'default' and _device_literals(kw.value) for kw in node.keywords):
            rule = 'DEVICE_DEFAULT'
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defaults = list(node.args.defaults) + [x for x in node.args.kw_defaults if x is not None]
            positional = list(node.args.posonlyargs) + list(node.args.args)
            pairs = list(zip(positional[len(positional) - len(node.args.defaults):], node.args.defaults))
            pairs += [(arg, value) for arg, value in zip(node.args.kwonlyargs, node.args.kw_defaults)
                      if value is not None]
            numeric_target = any(arg.arg in ('device', 'device_id', 'target', 'target_id', 'headset') and
                                 isinstance(value, ast.Constant) and type(value.value) is int
                                 for arg, value in pairs)
            if numeric_target or any(_device_literals(value) for value in defaults):
                rule = 'DEVICE_DEFAULT'
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            target = node.targets[0] if isinstance(node, ast.Assign) else node.target
            if node.value is not None:
                literal = _device_literals(node.value)
                generated = (any(isinstance(x, ast.Constant) and type(x.value) is str and
                                 x.value.startswith('cp_')
                                 for x in ast.walk(node.value)) and
                             any(isinstance(x, ast.Constant) and type(x.value) is int
                                 for x in ast.walk(node.value)) and
                             any(isinstance(x, (ast.List, ast.Tuple, ast.Set, ast.ListComp,
                                                ast.SetComp, ast.DictComp, ast.GeneratorExp)) or
                                 isinstance(x, ast.Call) and isinstance(x.func, ast.Name) and x.func.id == 'range'
                                 for x in ast.walk(node.value)))
                if literal and isinstance(node.value, ast.Constant):
                    rule = 'DEVICE_DEFAULT'
                elif literal or generated:
                    rule = 'DEVICE_TABLE'
        elif isinstance(node, ast.Constant) and type(node.value) is str:
            try:
                address = ipaddress.ip_address(node.value)
            except ValueError:
                pass
            else:
                if not address.is_loopback:
                    rule = 'RUNTIME_IP'
        if rule:
            name = node.name if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else symbol(node)
            found.add((rule, node.lineno, name))
    return sorted(found, key=lambda item: (item[1], item[0], item[2]))


def scan_scope(root, scope, *, now_utc=None):
    root = Path(root).absolute()
    require(not any(p.is_symlink() for p in (root, *root.parents)), 'Symlink root prohibited')
    now_utc = datetime.now(timezone.utc) if now_utc is None else now_utc
    require(isinstance(now_utc, datetime) and now_utc.utcoffset() == timezone.utc.utcoffset(now_utc),
            'Aware UTC audit clock required')
    chosen, excluded, exceptions = _enumerate(root, scope, now_utc)
    findings, debts = [], []
    for entry in chosen:
        text = _read_source(root, entry['path'])
        if not entry['path'].endswith('.py'):
            continue  # Explicit config/docs are not executable-device authorization.
        for rule, line, name in _findings(text):
            row = {'path': entry['path'], 'rule': rule, 'line': line, 'symbol': name}
            exception = exceptions.get((entry['path'], rule, name))
            if exception and exception['valid'] and exception['sha256'] == hashlib.sha256(text.encode('utf-8')).hexdigest():
                debts.append({**row, 'category': exception['category'], 'reason': exception['reason'],
                              'review': exception['review']})
            else:
                findings.append(row)
    return {'schema_version': 1, 'scope': 'explicit governance paths only',
            'opened_files': len(chosen), 'findings': findings, 'classified': debts,
            'excluded': excluded, 'passed': not findings,
            'integration_complete': not any(x['rule'] == 'NOT_INTEGRATED' for x in excluded)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scope', required=True)
    parser.add_argument('--root', default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args(argv)
    try:
        scope = strict_json_loads(Path(args.scope).read_text())
        result = scan_scope(args.root, scope)
    except (ValueError, TypeError, OSError, SyntaxError, UnicodeError):
        print(canonical_json({'error': 'SCOPE_REJECTED'}))
        return 2
    print(canonical_json(result))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
