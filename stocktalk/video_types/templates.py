"""Data-only local template assets: no imports, calls, URLs or executable markup."""
from html.parser import HTMLParser
import re

from jinja2 import StrictUndefined, nodes
from jinja2.sandbox import SandboxedEnvironment


class TypeTemplateEnvironment(SandboxedEnvironment):
    def is_safe_attribute(self, obj, attr, value):
        return False

    def is_safe_callable(self, obj):
        return False


class MarkupGuard(HTMLParser):
    def handle_starttag(self, tag, attrs):
        if tag in ('iframe', 'object', 'embed', 'base', 'form'):
            raise ValueError('executable template markup rejected')
        values = dict(attrs)
        if any(k.startswith('on') or k in ('srcdoc', 'action') for k, _ in attrs):
            raise ValueError('template event handler rejected')
        if tag == 'script' and values.get('src') not in ('gsap.min.js', 'stock-debate.js'):
            raise ValueError('only bundled scene runtime scripts are allowed')
        for key in ('src', 'href'):
            if key in values and values[key] not in ('gsap.min.js', 'stock-debate.js', 'stock-debate.css'):
                raise ValueError('template resource must use a registered local asset')


def template(source: str):
    if re.search(r'(?i)(https?:|file:|javascript:|@import|url\s*\()', source):
        raise ValueError('external template resources rejected')
    MarkupGuard().feed(source)
    env = TypeTemplateEnvironment(autoescape=True, undefined=StrictUndefined)
    env.globals.clear()
    env.filters = {k:v for k,v in env.filters.items() if k in ('escape', 'e', 'default', 'join', 'length')}
    tree = env.parse(source)
    if any(tree.find_all((nodes.Call, nodes.Import, nodes.FromImport, nodes.Include, nodes.Extends,
                         nodes.Mul, nodes.Pow, nodes.Assign, nodes.Macro))):
        raise ValueError('template supports data interpolation and bounded data loops only')
    return env.from_string(source)


def visual_template(declaration, kind, fallback):
    source = declaration.get('_assets', {}).get(kind)
    return template(source) if source is not None else fallback
