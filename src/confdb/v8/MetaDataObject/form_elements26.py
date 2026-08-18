"""Элементы форм 2.6 (слитый порт v8unpack FormElements26, MIT)."""
from .form_elements27 import FormProps as FormProps27, FormElement as FormElement27, Anchor
from .form_elements27 import Panel as Panel27
from .form_elements27 import FormElements27


# --- MetaDataObject/Form/FormElements26/FormElement.py ---
class FormProps(FormProps27):
    name_index = 3


# --- MetaDataObject/Form/FormElements26/Panel.py ---
class Panel(Panel27):
    ver = 26


# --- MetaDataObject/Form/FormElements26/FormElements26.py ---
class FormElements26(FormElements27):
    FormProps = FormProps
    Panel = Panel
