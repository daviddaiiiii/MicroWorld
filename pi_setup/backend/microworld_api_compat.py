"""Expose legacy API routes without creating a second scan or prompt store."""

from labthings.extensions import BaseExtension
from labthings.views import ActionView


def legacy_extension(extension_type, canonical_name, legacy_name, version):
    class LegacyExtension(BaseExtension):
        def __init__(self):
            super().__init__(legacy_name, description="Compatibility alias for " + canonical_name, version=version)
            extension_type.register_views(self)

        def add_view(self, view_class, *urls, **kwargs):
            # LabThings attaches the endpoint and extension owner to the view
            # class. A subclass keeps those attributes isolated from the main route.
            alias = type(view_class.__name__, (view_class,), {"__doc__": view_class.__doc__})
            if issubclass(view_class, ActionView):
                alias._deque = view_class._deque
            super().add_view(alias, *urls, **kwargs)
            alias.set_extension(canonical_name)

    return LegacyExtension
