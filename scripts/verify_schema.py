"""Plane schema verification — one-off ops script.

Dumps Django introspection data so we can replace assumptions about
Plane's model names with verified facts (see ТЗ 0.2 / SCHEMA.md).

Install: docker cp scripts/verify_schema.py \
    plane-ce-api-1:/code/plane/db/management/commands/verify_schema.py

Run:     docker compose exec api python manage.py verify_schema
"""

from django.apps import apps
from django.core.management.base import BaseCommand

CANDIDATE_KEYWORDS = (
    "issue",
    "work",
    "comment",
    "page",
    "project",
    "workspace",
    "member",
)

CONTENT_FIELD_HINTS = (
    "name",
    "title",
    "description",
    "description_html",
    "description_stripped",
    "description_binary",
    "comment_html",
    "comment_stripped",
    "content",
    "body",
)


class Command(BaseCommand):
    help = "Dump Plane Django model schema (apps, candidates, FKs, content fields)."

    def handle(self, *args, **opts):
        self.stdout.write("# === ALL INSTALLED APPS / MODELS ===\n")
        for app_config in sorted(apps.get_app_configs(), key=lambda a: a.label):
            model_names = sorted(m.__name__ for m in app_config.get_models())
            if not model_names:
                continue
            self.stdout.write(f"\n[{app_config.label}]  ({app_config.name})")
            for n in model_names:
                self.stdout.write(f"  - {n}")

        self.stdout.write("\n\n# === CANDIDATE MODELS (matching ТЗ 0.2 keywords) ===\n")
        candidates = []
        for model in apps.get_models():
            name_lc = model.__name__.lower()
            if any(k in name_lc for k in CANDIDATE_KEYWORDS):
                candidates.append(model)

        candidates.sort(key=lambda m: (m._meta.app_label, m.__name__))

        for model in candidates:
            meta = model._meta
            header = f"\n=== {meta.app_label}.{model.__name__} "
            header += f"(db_table={meta.db_table}) ==="
            self.stdout.write(header)

            fks_workspace = []
            fks_project = []
            content_fields = []
            other_fks = []

            for f in meta.get_fields():
                ftype = (
                    f.get_internal_type() if hasattr(f, "get_internal_type") else type(f).__name__
                )
                rel = getattr(f, "related_model", None)
                rel_name = rel.__name__ if rel is not None else None
                fname = f.name

                rel_suffix = f" -> {rel._meta.app_label}.{rel_name}" if rel else ""
                self.stdout.write(f"  {fname}: {ftype}{rel_suffix}")

                if rel is not None and ftype in {"ForeignKey", "OneToOneField"}:
                    if rel_name == "Workspace":
                        fks_workspace.append(fname)
                    elif rel_name == "Project":
                        fks_project.append(fname)
                    else:
                        other_fks.append(f"{fname}->{rel._meta.app_label}.{rel_name}")

                if fname in CONTENT_FIELD_HINTS and ftype in {
                    "CharField",
                    "TextField",
                    "JSONField",
                    "BinaryField",
                }:
                    content_fields.append(f"{fname}({ftype})")

            self.stdout.write("  --- summary ---")
            self.stdout.write(f"  FK -> Workspace : {fks_workspace or '—'}")
            self.stdout.write(f"  FK -> Project   : {fks_project or '—'}")
            self.stdout.write(f"  content fields  : {content_fields or '—'}")
            if other_fks:
                self.stdout.write(f"  other FKs (top) : {other_fks[:8]}")
