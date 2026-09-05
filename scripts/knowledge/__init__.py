"""Domínio de conhecimento canônico do wiki-ai (plano §4.2 e §5, onda W1).

Ponto de entrada típico:

    from knowledge.repository import Repository
    from knowledge.models import EntityDraft, EntityType

    repo = Repository.open("knowledge.db")
    with repo.revision(author="pipeline:ingest", reason="carga inicial") as rev:
        rev.put_entity(EntityDraft(...))

Só stdlib (`sqlite3`), sem import de `wk`/`codescan`/`sbindex`.
"""
