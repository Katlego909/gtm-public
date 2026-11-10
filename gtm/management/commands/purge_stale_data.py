"""
One-off cleanup of data left behind before deletion became permanent.

    python manage.py purge_stale_data           # report only
    python manage.py purge_stale_data --apply   # delete

1. Workspaces deactivated by the old soft delete (is_active=False). There is
   no restore flow, so nobody could reach them; their members asked for them
   to be deleted.
2. Uploaded files no row points to any more. Before gtm/storage_cleanup.py,
   deleting a row never removed its file. Only the upload folders this app
   writes to are scanned.
"""
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand

from gtm.account_deletion import delete_workspace
from gtm.models import ChatMessage, WorkspaceChatMessage
from gtm.models_workspace import Workspace
from gtm.storage_cleanup import FILE_MODELS

UPLOAD_PREFIXES = ("gtm/evidence", "gtm/delivery", "gtm/category", "workspace_resources", "agent_uploads")


def _walk(storage, prefix):
    try:
        dirs, files = storage.listdir(prefix)
    except (FileNotFoundError, NotADirectoryError):
        return
    for name in files:
        yield f"{prefix}/{name}"
    for d in dirs:
        yield from _walk(storage, f"{prefix}/{d}")


def referenced_file_names():
    names = set()
    for model in FILE_MODELS:
        names.update(n for n in model.objects.exclude(file="").values_list("file", flat=True) if n)
    for model in (ChatMessage, WorkspaceChatMessage):
        for attachments in model.objects.exclude(attachments=[]).values_list("attachments", flat=True):
            names.update(a.get("path") for a in attachments or [] if isinstance(a, dict) and a.get("path"))
    return names


class Command(BaseCommand):
    help = "Delete soft-deleted workspaces and uploaded files no row references (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Actually delete. Without it, only report.")

    def handle(self, *args, apply=False, **options):
        verb = "Deleting" if apply else "Would delete"

        stale = list(Workspace.objects.filter(is_active=False))
        self.stdout.write(f"{verb} {len(stale)} deactivated workspace(s).")
        for workspace in stale:
            self.stdout.write(f"  workspace {workspace.id} ({workspace.name})")
            if apply:
                delete_workspace(workspace)  # its files go via storage_cleanup on commit

        referenced = referenced_file_names()
        orphans = [name for prefix in UPLOAD_PREFIXES
                   for name in _walk(default_storage, prefix) if name not in referenced]
        self.stdout.write(f"{verb} {len(orphans)} orphaned file(s).")
        for name in orphans:
            self.stdout.write(f"  file {name}")
            if apply:
                default_storage.delete(name)
