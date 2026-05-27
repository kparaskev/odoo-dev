import ast


def _decorator_name(dec_node) -> str:
    if isinstance(dec_node, ast.Name):
        return dec_node.id
    if isinstance(dec_node, ast.Attribute):
        parts = []
        cur = dec_node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
        return ".".join(reversed(parts))
    if isinstance(dec_node, ast.Call):
        return _decorator_name(dec_node.func)
    return "unknown"


class OdooModelVisitor(ast.NodeVisitor):
    def __init__(self):
        self.models = []
        self.current_file = None

    def visit_ClassDef(self, node):
        is_odoo_model = False
        # Check parent classes/inheritance
        for base in node.bases:
            if isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name):
                if base.value.id == 'models' and base.attr in ('Model', 'TransientModel', 'AbstractModel'):
                    is_odoo_model = True
            elif isinstance(base, ast.Name):
                if base.id in ('Model', 'TransientModel', 'AbstractModel'):
                    is_odoo_model = True

        model_info = {
            "class_name": node.name,
            "model_name": None,
            "has_explicit_name": False,
            "inherit": None,
            "description": None,
            "fields": {},
            "methods": [],
            "file_path": self.current_file
        }
        has_odoo_attrs = False

        for item in node.body:
            if isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name):
                        if target.id == '_name':
                            model_info["model_name"] = self._get_value_literal(item.value)
                            model_info["has_explicit_name"] = True
                            has_odoo_attrs = True
                        elif target.id == '_inherit':
                            model_info["inherit"] = self._get_value_literal(item.value)
                            has_odoo_attrs = True
                        elif target.id == '_description':
                            model_info["description"] = self._get_value_literal(item.value)
                        
                        # Fields definition: field_name = fields.Char(...)
                        elif isinstance(item.value, ast.Call):
                            field_type = self._get_field_type(item.value.func)
                            if field_type:
                                field_name = target.id
                                model_info["fields"][field_name] = self._parse_field_call(field_type, item.value)
            
            elif isinstance(item, ast.FunctionDef):
                decorators = []
                for dec in item.decorator_list:
                    decorators.append(self._get_decorator_name(dec))
                
                args = [arg.arg for arg in item.args.args]
                model_info["methods"].append({
                    "name": item.name,
                    "args": args,
                    "decorators": decorators
                })

        if is_odoo_model or has_odoo_attrs:
            # If model_name is not explicitly set, but inherit is set, this is an extension
            if not model_info["model_name"] and model_info["inherit"]:
                if isinstance(model_info["inherit"], str):
                    model_info["model_name"] = model_info["inherit"]
                elif isinstance(model_info["inherit"], list) and len(model_info["inherit"]) > 0:
                    model_info["model_name"] = model_info["inherit"][0]
            
            self.models.append(model_info)

    def _get_value_literal(self, node):
        try:
            return ast.literal_eval(node)
        except Exception:
            # For complex Python expressions (like lambda, dynamic calls), return its raw AST string representation
            if isinstance(node, ast.Name):
                return node.id
            return None

    def _get_field_type(self, func_node):
        if isinstance(func_node, ast.Attribute) and isinstance(func_node.value, ast.Name):
            if func_node.value.id == 'fields':
                return func_node.attr
        elif isinstance(func_node, ast.Name):
            if func_node.id in ('Char', 'Text', 'Html', 'Integer', 'Float', 'Monetary', 'Boolean',
                                'Date', 'Datetime', 'Binary', 'Selection', 'Reference',
                                'Many2one', 'One2many', 'Many2many'):
                return func_node.id
        return None

    def _parse_field_call(self, field_type, call_node):
        field_data = {
            "type": field_type,
            "string": None,
            "relation": None,
            "required": False,
            "readonly": False,
            "attrs": {}
        }
        
        # Positional arguments: string or comodel_name
        pos_args = []
        for arg in call_node.args:
            lit = self._get_value_literal(arg)
            if lit is not None:
                pos_args.append(lit)

        if field_type in ('Many2one', 'One2many', 'Many2many') and pos_args:
            field_data["relation"] = pos_args[0]
            if len(pos_args) > 1:
                field_data["string"] = pos_args[1]
        elif pos_args:
            field_data["string"] = pos_args[0]

        # Keyword arguments
        for kw in call_node.keywords:
            val = self._get_value_literal(kw.value)
            if kw.arg == 'string':
                field_data["string"] = val
            elif kw.arg == 'comodel_name':
                field_data["relation"] = val
            elif kw.arg == 'required':
                field_data["required"] = bool(val)
            elif kw.arg == 'readonly':
                field_data["readonly"] = bool(val)
            else:
                if val is not None:
                    field_data["attrs"][kw.arg] = val

        return field_data

    def _get_decorator_name(self, dec_node):
        if isinstance(dec_node, ast.Name):
            return dec_node.id
        elif isinstance(dec_node, ast.Attribute):
            parts = []
            current = dec_node
            while isinstance(current, ast.Attribute):
                parts.append(current.attr)
                current = current.value
            if isinstance(current, ast.Name):
                parts.append(current.id)
            return ".".join(reversed(parts))
        elif isinstance(dec_node, ast.Call):
            return self._get_decorator_name(dec_node.func)
        return "unknown"


class OdooModelIndexVisitor(ast.NodeVisitor):
    """Minimal visitor that collects (model_name, file_path) pairs only.

    Much faster than OdooModelVisitor — skips fields, methods, and decorators.
    Each entry in *entries* represents one (model, file) relationship; a class
    that uses _inherit = ['a', 'b'] produces two entries, one per model.
    """

    def __init__(self):
        self.entries: list[tuple[str, str]] = []
        self.current_file: str | None = None

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        is_odoo_base = False
        for base in node.bases:
            if isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name):
                if base.value.id == "models" and base.attr in (
                    "Model", "TransientModel", "AbstractModel"
                ):
                    is_odoo_base = True
            elif isinstance(base, ast.Name):
                if base.id in ("Model", "TransientModel", "AbstractModel"):
                    is_odoo_base = True

        name_val = None
        inherit_val = None
        for item in node.body:
            if not isinstance(item, ast.Assign):
                continue
            for target in item.targets:
                if not isinstance(target, ast.Name):
                    continue
                if target.id == "_name" and name_val is None:
                    try:
                        name_val = ast.literal_eval(item.value)
                    except Exception:
                        pass
                elif target.id == "_inherit" and inherit_val is None:
                    try:
                        inherit_val = ast.literal_eval(item.value)
                    except Exception:
                        pass

        if not (is_odoo_base or name_val is not None or inherit_val is not None):
            self.generic_visit(node)
            return

        if name_val:
            models = [name_val]
        elif isinstance(inherit_val, str):
            models = [inherit_val]
        elif isinstance(inherit_val, list):
            models = [m for m in inherit_val if isinstance(m, str)]
        else:
            models = []

        if self.current_file:
            for model_name in models:
                self.entries.append((model_name, self.current_file))


class OdooModelMethodVisitor(ast.NodeVisitor):
    """Extracts methods from classes that define or extend *target_model*.

    Only visits classes whose ``_name`` matches *target_model* or whose
    ``_inherit`` includes it.  Each item in *methods* is::

        {
          "class_name": str,
          "name":       str,   # Python method name
          "args":       list[str],
          "decorators": list[str],
        }

    The caller is responsible for attaching module / file context.
    """

    def __init__(self, target_model: str) -> None:
        self.target_model = target_model
        self.methods: list[dict] = []
        self.current_file: str | None = None

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        name_val = None
        inherit_val = None
        for item in node.body:
            if not isinstance(item, ast.Assign):
                continue
            for target in item.targets:
                if not isinstance(target, ast.Name):
                    continue
                if target.id == "_name" and name_val is None:
                    try:
                        name_val = ast.literal_eval(item.value)
                    except Exception:
                        pass
                elif target.id == "_inherit" and inherit_val is None:
                    try:
                        inherit_val = ast.literal_eval(item.value)
                    except Exception:
                        pass

        relates = (
            name_val == self.target_model
            or inherit_val == self.target_model
            or (isinstance(inherit_val, list) and self.target_model in inherit_val)
        )
        if not relates:
            self.generic_visit(node)
            return

        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.methods.append({
                    "class_name": node.name,
                    "name": item.name,
                    "args": [arg.arg for arg in item.args.args],
                    "decorators": [_decorator_name(d) for d in item.decorator_list],
                })
