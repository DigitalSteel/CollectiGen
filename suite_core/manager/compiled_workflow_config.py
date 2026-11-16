"""Qualified public workflow release profiles.

Populated only after independent packages, signed metadata and transport have
been qualified and published. An empty mapping never advertises a development
build as an available update. No private signing material belongs in this file.
"""
from types import MappingProxyType

PROFILES = MappingProxyType({})
