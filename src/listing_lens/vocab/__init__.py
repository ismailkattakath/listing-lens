"""The service vocabulary's shape (the vocabulary itself is the operator's DATA, never in git).

Two SKOS schemes may share the file: the SERVICES scheme (whose owl:versionInfo is the vocabulary version) and
the STOP scheme <urn:li:vocab:stop:> (analyst's representation, 2026-11-08): phrases the matcher matches like
any label and then DROPS, together with every service span they overlap ("google duo" is a video-call app,
not a service). rdflib only, so export-db can use it without loading spaCy.
"""

from __future__ import annotations

import rdflib
from rdflib.namespace import RDF, SKOS

STOP_SCHEME = rdflib.URIRef("urn:li:vocab:stop:")


def is_stop(graph: rdflib.Graph, concept) -> bool:
    return (concept, SKOS.inScheme, STOP_SCHEME) in graph


def service_schemes(graph: rdflib.Graph) -> list:
    """Every ConceptScheme but the stop scheme, in a fixed order (rdflib's iteration order is not)."""
    return sorted((s for s in graph.subjects(RDF.type, SKOS.ConceptScheme) if s != STOP_SCHEME), key=str)


def service_concepts(graph: rdflib.Graph) -> list:
    return sorted((c for c in set(graph.subjects(RDF.type, SKOS.Concept)) if not is_stop(graph, c)), key=str)


def notation(graph: rdflib.Graph, concept) -> str:
    """The concept key li writes: skos:notation, else the IRI's last ':' segment."""
    return str(graph.value(concept, SKOS.notation) or str(concept).rsplit(":", 1)[-1])
