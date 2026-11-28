import os

from collector import ROOT

KEY_NAMES = ('GEMINI_API_KEY', 'LAW_API_OC', 'MOLIT_SERVICE_KEY')


def load_local_environment():
    """Load the allowed local settings, with process variables taking precedence."""
    dotenv = ROOT / '.env'
    if dotenv.is_file():
        for line in dotenv.read_text(encoding='utf-8-sig').splitlines():
            name, separator, value = line.partition('=')
            name, value = name.strip(), value.strip()
            if not separator or name not in (*KEY_NAMES, 'GEMINI_MODEL') or os.environ.get(name, '').strip():
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            if value:
                os.environ[name] = value
    return [name for name in KEY_NAMES if not os.environ.get(name, '').strip()]
