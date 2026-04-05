"""
Test suite for session-level access control.
Validates that users cannot access sessions they don't own.
"""

from django.test import TestCase, Client
from django.contrib.auth import get_user_model
from django.urls import reverse
from uuid import uuid4

from gtm.models import AssessmentSession, Response, Question, Category, ResultSnapshot
from gtm.models_workspace import Workspace, WorkspaceMembership

User = get_user_model()


class SessionAccessControlTests(TestCase):
    """Test suite for session access control across endpoints."""

    def setUp(self):
        """Set up test users, workspaces, and sessions."""
        self.client = Client()
        
        # Create test users
        self.user1 = User.objects.create_user(username='user1', password='pass1', email='user1@test.com')
        self.user2 = User.objects.create_user(username='user2', password='pass2', email='user2@test.com')
        self.user3 = User.objects.create_user(username='user3', password='pass3', email='user3@test.com')
        
        # Create workspaces
        self.workspace1 = Workspace.objects.create(name='Workspace 1')
        self.workspace2 = Workspace.objects.create(name='Workspace 2')
        
        # Add users to workspaces
        WorkspaceMembership.objects.create(
            user=self.user1,
            workspace=self.workspace1,
            role='admin',
            is_active=True
        )
        WorkspaceMembership.objects.create(
            user=self.user2,
            workspace=self.workspace2,
            role='admin',
            is_active=True
        )
        
        # Create category for responses
        self.category = Category.objects.create(
            name='Test Category',
            weight=1.0
        )
        
        # Create question
        self.question = Question.objects.create(
            id_code='TEST1',
            text='Test Question',
            category=self.category,
            weight=1.0
        )
        
        # Session 1: User1 owns, in Workspace1
        self.session1 = AssessmentSession.objects.create(
            user=self.user1,
            owner_client_id='client_1',
            workspace=self.workspace1,
            is_completed=True
        )
        
        # Session 2: User2 owns, in Workspace2
        self.session2 = AssessmentSession.objects.create(
            user=self.user2,
            owner_client_id='client_2',
            workspace=self.workspace2,
            is_completed=True
        )
        
        # Session 3: Anonymous (client_1), no workspace
        self.session3 = AssessmentSession.objects.create(
            user=None,
            owner_client_id='client_1',
            workspace=None,
            is_completed=True
        )
        
        # Session 4: Anonymous (client_2), no workspace
        self.session4 = AssessmentSession.objects.create(
            user=None,
            owner_client_id='client_2',
            workspace=None,
            is_completed=True
        )
        
        # Add responses to sessions for testing
        for session in [self.session1, self.session2, self.session3, self.session4]:
            Response.objects.create(
                session=session,
                question=self.question,
                score=3,
                context_note='Test response'
            )
        
        # Create result snapshots
        for session in [self.session1, self.session2]:
            ResultSnapshot.objects.create(
                session=session,
                overall=3.0,
                category_breakdown=[],
                radar_labels=[],
                radar_values=[],
                ai_playbook='# Test Playbook'
            )

    def test_user1_can_access_own_session(self):
        """Authenticated user can access their own session."""
        self.client.login(username='user1', password='pass1')
        
        response = self.client.get(reverse('gtm:results', args=[self.session1.uuid]))
        self.assertEqual(response.status_code, 200)

    def test_user2_cannot_access_user1_session(self):
        """Authenticated user cannot access another user's session."""
        self.client.login(username='user2', password='pass2')
        
        response = self.client.get(reverse('gtm:results', args=[self.session1.uuid]))
        self.assertEqual(response.status_code, 403)

    def test_user3_cannot_access_any_workspace_session(self):
        """User outside workspace cannot access workspace sessions."""
        self.client.login(username='user3', password='pass3')
        
        response = self.client.get(reverse('gtm:results', args=[self.session1.uuid]))
        self.assertEqual(response.status_code, 403)
        
        response = self.client.get(reverse('gtm:results', args=[self.session2.uuid]))
        self.assertEqual(response.status_code, 403)

    def test_different_client_ids_denied_anonymous(self):
        """Anonymous user with different client_id cannot access session."""
        # Try to access session3 (client_1) with client_2 cookie
        self.client.cookies['gtm_client'] = 'client_2'
        
        response = self.client.get(reverse('gtm:results', args=[self.session3.uuid]))
        self.assertEqual(response.status_code, 403)

    def test_correct_client_id_still_denied_anonymous_access(self):
        """Anonymous users are denied even when client_id matches."""
        # Access session3 (client_1) with matching client_1 cookie
        self.client.cookies['gtm_client'] = 'client_1'

        response = self.client.get(reverse('gtm:results', args=[self.session3.uuid]))
        self.assertEqual(response.status_code, 403)

    def test_playbook_endpoint_access_control(self):
        """Playbook endpoint enforces access control."""
        self.client.login(username='user2', password='pass2')
        
        response = self.client.get(reverse('gtm:playbook', args=[self.session1.uuid]))
        self.assertEqual(response.status_code, 403)

    def test_download_pdf_endpoint_access_control(self):
        """PDF download endpoint enforces access control."""
        self.client.login(username='user2', password='pass2')
        
        response = self.client.get(reverse('gtm:download', args=[self.session1.uuid]))
        self.assertEqual(response.status_code, 403)

    def test_chat_endpoint_access_control(self):
        """Chat endpoint enforces access control."""
        self.client.login(username='user2', password='pass2')
        
        response = self.client.get(reverse('gtm:chat', args=[self.session1.uuid]))
        self.assertEqual(response.status_code, 403)

    def test_chat_api_endpoint_access_control(self):
        """Chat API endpoint enforces access control."""
        self.client.login(username='user2', password='pass2')
        
        response = self.client.post(
            reverse('gtm:chat_api', args=[self.session1.uuid]),
            data='{"message": "test"}',
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn('error', response.json())

    def test_insight_status_endpoint_access_control(self):
        """Insight status endpoint enforces access control."""
        self.client.login(username='user2', password='pass2')
        
        response_obj = Response.objects.filter(session=self.session1).first()
        response = self.client.get(
            reverse('gtm:insight_status', args=[self.session1.uuid, response_obj.id])
        )
        self.assertEqual(response.status_code, 403)

    def test_playbook_status_endpoint_access_control(self):
        """Playbook status endpoint enforces access control."""
        self.client.login(username='user2', password='pass2')
        
        response = self.client.get(reverse('gtm:playbook_status', args=[self.session1.uuid]))
        self.assertEqual(response.status_code, 403)

    def test_invalid_session_uuid(self):
        """Invalid session UUID returns 403."""
        self.client.login(username='user1', password='pass1')
        
        fake_uuid = uuid4()
        response = self.client.get(reverse('gtm:results', args=[fake_uuid]))
        self.assertEqual(response.status_code, 403)

    def test_workspace_member_can_access_workspace_session(self):
        """Workspace member can access sessions in their workspace."""
        # Add user3 to workspace1
        WorkspaceMembership.objects.create(
            user=self.user3,
            workspace=self.workspace1,
            role='member',
            is_active=True
        )
        
        self.client.login(username='user3', password='pass3')
        
        # Now user3 should be able to access session1 (in workspace1)
        response = self.client.get(reverse('gtm:results', args=[self.session1.uuid]))
        self.assertEqual(response.status_code, 200)

    def test_inactive_workspace_member_denied(self):
        """Inactive workspace member cannot access workspace sessions."""
        # Add user3 to workspace1 but mark as inactive
        WorkspaceMembership.objects.create(
            user=self.user3,
            workspace=self.workspace1,
            role='member',
            is_active=False
        )
        
        self.client.login(username='user3', password='pass3')
        
        response = self.client.get(reverse('gtm:results', args=[self.session1.uuid]))
        self.assertEqual(response.status_code, 403)

    def test_session_without_workspace_owner_only(self):
        """Session without workspace can only be accessed by owner."""
        # Create a user-owned session without workspace
        session_no_ws = AssessmentSession.objects.create(
            user=self.user1,
            owner_client_id='client_no_ws',
            workspace=None,
            is_completed=True
        )
        
        # user1 can access (owns it)
        self.client.login(username='user1', password='pass1')
        response = self.client.get(reverse('gtm:results', args=[session_no_ws.uuid]))
        self.assertEqual(response.status_code, 200)
        
        # user2 cannot access (not owner, no workspace)
        self.client.login(username='user2', password='pass2')
        response = self.client.get(reverse('gtm:results', args=[session_no_ws.uuid]))
        self.assertEqual(response.status_code, 403)

    def test_unauthenticated_access_to_authenticated_sessions(self):
        """Unauthenticated user cannot access authenticated user sessions."""
        # Try to access user1's session without login
        response = self.client.get(reverse('gtm:results', args=[self.session1.uuid]))
        self.assertEqual(response.status_code, 403)
