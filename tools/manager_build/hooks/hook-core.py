"""Build-only declaration of source-verified runtime facade attributes."""
from pathlib import Path
import ast,json

def hook(api):
    facade=Path(api.module.filename)
    tree=ast.parse(facade.read_bytes());groups=None
    for node in tree.body:
        if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name)and t.id=='_EXPORT_GROUPS'for t in node.targets):
            groups=ast.literal_eval(node.value);break
    if groups is None:raise ValueError('explicit facade export groups required')
    exports={name:(owner,symbol)for owner,items in groups for name,symbol in items}
    declarations=[]
    for raw,origin,kind in list(api.module_graph.make_missing_toc()):
        name=raw.strip('\"\'')
        if not name.startswith('core.')or name.count('.')!=1 or name[5:]not in exports:continue
        owner,symbol=exports[name[5:]]
        path=facade.parent/(owner.removeprefix('.')+'.py')
        if not path.is_file():continue # genuine missing owner stays held
        nodes=ast.parse(path.read_bytes()).body;defined=set()
        for node in nodes:
            if isinstance(node,(ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef)):defined.add(node.name)
            elif isinstance(node,(ast.Import,ast.ImportFrom)):
                defined.update(alias.asname or alias.name.split('.')[0]for alias in node.names)
            elif isinstance(node,(ast.Assign,ast.AnnAssign)):
                targets=node.targets if isinstance(node,ast.Assign)else[node.target]
                for target in targets:defined.update(n.id for n in ast.walk(target)if isinstance(n,ast.Name))
        if symbol not in defined:raise ValueError('facade owner symbol not proved: '+name)
        api.module.add_global_attr(name[5:])
        api.add_imports('core'+owner)
        node=api.module_graph.find_node(raw)
        if node is not None:api.module_graph.removeNode(node)
        declarations.append({'attribute':name,'owner':'core'+owner,'symbol':symbol,'owner_file':str(path)})
    (Path(__file__).resolve().parents[1]/'FACADE_DECLARATIONS.json').write_text(json.dumps(declarations,indent=2))
