"""One-time edit: create ordinary importable modules, not runtime code capsules."""
import ast
from pathlib import Path
import textwrap

root=Path('toolchain/codex_wire_audit/extractors')
p=root/'local_storage.py'
s=p.read_text(); lines=s.splitlines(keepends=True); tree=ast.parse(s)
cls=next(n for n in tree.body if isinstance(n,ast.ClassDef))
func=next(n for n in cls.body if isinstance(n,ast.FunctionDef))
payload=next(n for n in func.body if isinstance(n,ast.AnnAssign) and isinstance(n.target,ast.Name) and n.target.id=='semantic_payload')

def write(name,txt):
    (root/name).write_text(txt.rstrip()+'\n')

def span(first,last): return ''.join(lines[first-1:last])

write('local_storage_specs.py', '"""Source identities and reviewed markers for local-storage extraction."""\n\nfrom __future__ import annotations\n\n'+span(18,177))
write('local_storage_sources.py', '''"""Source parsing and fail-visible evidence checks for local storage."""
from __future__ import annotations

import re
from typing import Any

from ..diagnostics import DiagnosticCollector
from ..models import SourceFile, SourceSnapshot
from .local_storage_specs import EXTRACTOR_ID, SOURCE_IDS, _DB_SPECS, _REQUIRED_MARKERS

'''+span(180,334))

# Split the declarative model by responsibility. Build each section through
# keyword-only arguments, so static tooling sees every dependency.
values={k.value:v for k,v in zip(payload.value.keys,payload.value.values)}
known={n.id for n in ast.walk(ast.Module(body=func.body[:func.body.index(payload)],type_ignores=[])) if isinstance(n,ast.Name) and isinstance(n.ctx,ast.Store)}
builders=[]
for filename,keys in [
 ('local_storage_layout.py', ['scope','roots','identity_domains','rollouts','databases']),
 ('local_storage_lifecycle.py', ['transitions','sidecars','invariants']),
]:
    parts=['"""Pure local-storage '+('layout' if 'layout' in filename else 'lifecycle')+' projections from resolved source values."""\n\nfrom __future__ import annotations\n\nfrom typing import Any\n']
    for key in keys:
        value=values[key]
        names=sorted({n.id for n in ast.walk(value) if isinstance(n,ast.Name) and isinstance(n.ctx,ast.Load)} & known)
        # Source segments have an unindented first line and preserve the
        # original column on following lines. Normalize only that indentation.
        segment=ast.get_source_segment(s,value)
        seglines=segment.splitlines()
        indent=min(len(line)-len(line.lstrip()) for line in seglines[1:] if line.strip())
        normalized='\n'.join([seglines[0]]+[line[indent:] if line.strip() else '' for line in seglines[1:]])
        params=', '.join(name+': Any' for name in names)
        signature='*, '+params if names else ''
        parts.append(f'\n\ndef build_{key}({signature}) -> '+('list[dict[str, Any]]' if isinstance(value,ast.List) else 'dict[str, Any]')+':\n'+textwrap.indent('return '+normalized,'    ')+'\n')
        builders.append((key,names))
    write(filename,''.join(parts))

imports='''"""Codex local thread-storage extraction with explicit, independently testable modules."""
from __future__ import annotations

import re
from typing import Any

from ..diagnostics import DiagnosticCollector
from ..models import SourceSnapshot, semantic_fingerprint
from .registry import ExtractorResult, register_extractor
from .local_storage_specs import EXTRACTOR_ID, SCHEMA_VERSION, SOURCE_IDS
from .local_storage_sources import (
    _source, _emit, _check_sources, _const_or_expected,
    _duration_product, _database_catalog, _integer_call,
)
from .local_storage_layout import (
    build_scope, build_roots, build_identity_domains, build_rollouts, build_databases,
)
from .local_storage_lifecycle import build_transitions, build_sidecars, build_invariants


'''
replacement='        semantic_payload: dict[str, Any] = {\n'
for key,names in builders:
    args=', '.join(n+'='+n for n in names)
    if len(args)>75:
        call=f'build_{key}(\n'+''.join('                '+n+'='+n+',\n' for n in names)+'            )'
    else: call=f'build_{key}({args})'
    replacement+=f'            "{key}": {call},\n'
replacement+='        }\n'
write('local_storage.py',imports+span(cls.lineno,payload.lineno-1)+replacement+span(payload.end_lineno+1,len(lines)))
for filename in ['local_storage.py','local_storage_specs.py','local_storage_sources.py','local_storage_layout.py','local_storage_lifecycle.py']:
    txt=(root/filename).read_text();ast.parse(txt);print(filename,len(txt.splitlines()))
