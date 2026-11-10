"""Self-serve account deletion. The rules live in gtm/account_deletion.py."""
from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from gtm.account_deletion import (
    AccountDeletionBlocked, delete_account, sole_member_workspaces, workspaces_blocking_deletion,
)
from gtm.models_workspace import WorkspaceMembership


def _confirmation_ok(user, data):
    """Password if the account has one; otherwise the account's email (or username)."""
    if user.has_usable_password():
        return user.check_password(data.get("password", ""))
    expected = (user.email or user.username).strip().lower()
    return data.get("confirm_identity", "").strip().lower() == expected


@login_required
@require_http_methods(["GET", "POST"])
def delete_account_view(request):
    user = request.user
    error = ""

    if request.method == "POST":
        if not _confirmation_ok(user, request.POST):
            error = ("That password is not correct." if user.has_usable_password()
                     else "That does not match your account's email address.")
        else:
            try:
                delete_account(user)
            except AccountDeletionBlocked:
                error = "A workspace you run still needs another admin. See below."
            else:
                logout(request)
                messages.success(request, "Your account and its data have been deleted.")
                return redirect("gtm:landing")

    sole = sole_member_workspaces(user)
    sole_ids = {w.id for w in sole}
    shared = [
        m.workspace for m in WorkspaceMembership.objects.filter(
            user=user, is_active=True, workspace__is_active=True).select_related("workspace")
        if m.workspace.id not in sole_ids
    ]
    return render(request, "dashboard/delete_account.html", {
        "blocking": workspaces_blocking_deletion(user),
        "sole_workspaces": sole,
        "shared_workspaces": shared,
        "needs_password": user.has_usable_password(),
        "identity_hint": user.email or user.username,
        "error": error,
    })
