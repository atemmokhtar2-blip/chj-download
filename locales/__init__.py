from . import en, ar

LOCALES = {
    "en": en.STRINGS,
    "ar": ar.STRINGS,
}

def t(lang: str, key: str, **kwargs) -> str:
    strings = LOCALES.get(lang, LOCALES["en"])
    text = strings.get(key, LOCALES["en"].get(key, key))
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, ValueError):
            return text
    return text
