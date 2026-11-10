"""
Remove uploaded files from storage when the rows that own them go away.

Django never deletes a stored file when its model row is deleted, so every
deleted evidence document, resource, or chat attachment used to stay in the
GCS bucket forever. Retention rule: an uploaded file lives exactly as long as
the row that owns it. post_delete also fires for rows removed by cascade
(deleting a workspace, an assessment, or an account), so this one place covers
every deletion path.

Files are deleted only after the transaction commits, so a rolled-back delete
never loses a file whose row still exists. A failed storage delete is logged,
not raised: the row is already gone and the user's action succeeded.
"""
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models.signals import post_delete, pre_save

from dashboard.models import Resource
from .models import CategoryDocument, ChatMessage, DeliveryDocument, GTMFile, WorkspaceChatMessage
from .utils_logging import log_error

FILE_MODELS = (GTMFile, DeliveryDocument, CategoryDocument, Resource)
ATTACHMENT_MODELS = (ChatMessage, WorkspaceChatMessage)


def delete_stored_file_on_commit(storage, name):
    if not name:
        return

    def _delete():
        try:
            storage.delete(name)
        except Exception as exc:  # noqa: BLE001 - see module docstring
            log_error("Storage cleanup", exc, {"name": name})

    transaction.on_commit(_delete)


def _delete_file_field(sender, instance, **kwargs):
    if instance.file:
        delete_stored_file_on_commit(instance.file.storage, instance.file.name)


def _delete_chat_attachments(sender, instance, **kwargs):
    # Agent chat uploads are saved straight to default_storage and only their
    # path is kept, in the message's attachments JSON
    # (dashboard/document_processors.py::_save_agent_attachment).
    for attachment in instance.attachments or []:
        if isinstance(attachment, dict):
            delete_stored_file_on_commit(default_storage, attachment.get("path"))


def _delete_replaced_resource_file(sender, instance, **kwargs):
    # The resource edit form can swap the file; the old one would be orphaned.
    if not instance.pk:
        return
    update_fields = kwargs.get("update_fields")
    if update_fields is not None and "file" not in update_fields:
        return
    old_name = Resource.objects.filter(pk=instance.pk).values_list("file", flat=True).first()
    if old_name and old_name != (instance.file.name if instance.file else ""):
        delete_stored_file_on_commit(instance.file.storage, old_name)


def connect():
    for model in FILE_MODELS:
        post_delete.connect(_delete_file_field, sender=model, dispatch_uid=f"storage_cleanup.file.{model._meta.label}")
    for model in ATTACHMENT_MODELS:
        post_delete.connect(_delete_chat_attachments, sender=model,
                            dispatch_uid=f"storage_cleanup.attachments.{model._meta.label}")
    pre_save.connect(_delete_replaced_resource_file, sender=Resource, dispatch_uid="storage_cleanup.resource_replace")
