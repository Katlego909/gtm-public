"""Guards for the modal markup that the dashboard shell ships.

Two things are checked here, both learned the hard way.

First, that no raw template syntax survives into the response. A multi-line
``{# #}`` is not a Django comment -- it renders as literal text -- and one placed
inside an ``<input>`` tag shipped a field that could not carry its own value.
Asserting that some expected substring is present does not catch that, because
the substring is still there inside the broken tag; asserting that no template
delimiter is *absent* does.

Second, that the create-workspace form is genuinely inlined. Its GET view runs
zero queries and returns static markup, so it is shipped with the page rather
than fetched -- users are a ~400ms round trip from the region, and that request
bought nothing.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse

from gtm.models_workspace import Workspace, WorkspaceMembership

User = get_user_model()


@override_settings(SECURE_SSL_REDIRECT=False)
class ModalMarkupTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="modaluser", password="pass1234", email="modal@test.com"
        )
        cls.workspace = Workspace.objects.create(name="Modal Co")
        WorkspaceMembership.objects.create(
            workspace=cls.workspace, user=cls.user, role="admin", is_active=True
        )

    def setUp(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["current_workspace_id"] = str(self.workspace.id)
        session.save()

    # Pages carrying modal triggers, one per shell area.
    PAGES = ["dashboard", "tasks_board", "workspace_hub", "asset_library", "agent_hub"]

    def test_no_raw_template_syntax_reaches_the_browser(self):
        for name in self.PAGES:
            with self.subTest(page=name):
                html = self.client.get(reverse(name)).content.decode()
                self.assertNotIn("{#", html)
                self.assertNotIn("{%", html)
                self.assertNotIn("{{", html)

    def test_modal_helpers_are_defined_for_every_shell_page(self):
        """openModal reveals onto a skeleton; both helpers must be present."""
        for name in self.PAGES:
            with self.subTest(page=name):
                html = self.client.get(reverse(name)).content.decode()
                self.assertIn("data-modal-skeleton", html)
                self.assertIn("window.modalSkeleton", html)
                self.assertIn("window.modalError", html)

    def test_hub_ships_the_create_workspace_form_inline(self):
        html = self.client.get(reverse("workspace_hub")).content.decode()

        self.assertIn('id="hub-create-ws-template"', html)
        # The include must render the form branch, not the post-success branch.
        start = html.index('id="hub-create-ws-template"')
        end = html.index("</template>", start)
        template_body = html[start:end]
        self.assertIn(reverse("create_workspace_dashboard"), template_body)
        self.assertIn('name="name"', template_body)
        self.assertIn("csrfmiddlewaretoken", template_body)
        self.assertNotIn("Workspace Created!", template_body)

    def test_create_workspace_buttons_point_at_the_inline_template(self):
        html = self.client.get(reverse("workspace_hub")).content.decode()
        self.assertIn('data-inline-template="hub-create-ws-template"', html)

    def test_settling_fade_is_scoped_away_from_modal_content(self):
        """The 200ms fade on top of the round trip was pure dead time."""
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn(".htmx-settling:not(#modal-content)", html)
