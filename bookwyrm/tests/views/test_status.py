""" test for app action functionality """
import json
from unittest.mock import patch
from django.test import TestCase
from django.test.client import RequestFactory

from bookwyrm import forms, models, views
from bookwyrm.settings import DOMAIN


# pylint: disable=invalid-name
@patch("bookwyrm.suggested_users.rerank_suggestions_task.delay")
@patch("bookwyrm.activitystreams.populate_stream_task.delay")
@patch("bookwyrm.activitystreams.remove_status_task.delay")
@patch("bookwyrm.models.activitypub_mixin.broadcast_task.delay")
class StatusViews(TestCase):
    """viewing and creating statuses"""

    def setUp(self):
        """we need basic test data and mocks"""
        self.factory = RequestFactory()
        with patch("bookwyrm.suggested_users.rerank_suggestions_task.delay"), patch(
            "bookwyrm.activitystreams.populate_stream_task.delay"
        ):
            self.local_user = models.User.objects.create_user(
                "mouse@local.com",
                "mouse@mouse.com",
                "mouseword",
                local=True,
                localname="mouse",
                remote_id="https://example.com/users/mouse",
            )
        with patch("bookwyrm.models.user.set_remote_server"):
            self.remote_user = models.User.objects.create_user(
                "rat",
                "rat@email.com",
                "ratword",
                local=False,
                remote_id="https://example.com/users/rat",
                inbox="https://example.com/users/rat/inbox",
                outbox="https://example.com/users/rat/outbox",
            )

        work = models.Work.objects.create(title="Test Work")
        self.book = models.Edition.objects.create(
            title="Example Edition",
            remote_id="https://example.com/book/1",
            parent_work=work,
        )
        models.SiteSettings.objects.create()

    def test_handle_status(self, *_):
        """create a status"""
        view = views.CreateStatus.as_view()
        form = forms.CommentForm(
            {
                "content": "hi",
                "user": self.local_user.id,
                "book": self.book.id,
                "privacy": "public",
            }
        )
        request = self.factory.post("", form.data)
        request.user = self.local_user

        view(request, "comment")

        status = models.Comment.objects.get()
        self.assertEqual(status.content, "<p>hi</p>")
        self.assertEqual(status.user, self.local_user)
        self.assertEqual(status.book, self.book)

    def test_handle_status_reply(self, *_):
        """create a status in reply to an existing status"""
        view = views.CreateStatus.as_view()
        user = models.User.objects.create_user(
            "rat", "rat@rat.com", "password", local=True
        )
        parent = models.Status.objects.create(
            content="parent status", user=self.local_user
        )
        form = forms.ReplyForm(
            {
                "content": "hi",
                "user": user.id,
                "reply_parent": parent.id,
                "privacy": "public",
            }
        )
        request = self.factory.post("", form.data)
        request.user = user

        view(request, "reply")

        status = models.Status.objects.get(user=user)
        self.assertEqual(status.content, "<p>hi</p>")
        self.assertEqual(status.user, user)
        self.assertEqual(models.Notification.objects.get().user, self.local_user)

    def test_handle_status_mentions(self, *_):
        """@mention a user in a post"""
        view = views.CreateStatus.as_view()
        user = models.User.objects.create_user(
            f"rat@{DOMAIN}",
            "rat@rat.com",
            "password",
            local=True,
            localname="rat",
        )
        form = forms.CommentForm(
            {
                "content": "hi @rat",
                "user": self.local_user.id,
                "book": self.book.id,
                "privacy": "public",
            }
        )
        request = self.factory.post("", form.data)
        request.user = self.local_user

        view(request, "comment")

        status = models.Status.objects.get()
        self.assertEqual(list(status.mention_users.all()), [user])
        self.assertEqual(models.Notification.objects.get().user, user)
        self.assertEqual(
            status.content, f'<p>hi <a href="{user.remote_id}">@rat</a></p>'
        )

    def test_handle_status_reply_with_mentions(self, *_):
        """reply to a post with an @mention'ed user"""
        view = views.CreateStatus.as_view()
        user = models.User.objects.create_user(
            "rat", "rat@rat.com", "password", local=True, localname="rat"
        )
        form = forms.CommentForm(
            {
                "content": "hi @rat@example.com",
                "user": self.local_user.id,
                "book": self.book.id,
                "privacy": "public",
            }
        )
        request = self.factory.post("", form.data)
        request.user = self.local_user

        view(request, "comment")
        status = models.Status.objects.get()

        form = forms.ReplyForm(
            {
                "content": "right",
                "user": user.id,
                "privacy": "public",
                "reply_parent": status.id,
            }
        )
        request = self.factory.post("", form.data)
        request.user = user

        view(request, "reply")

        reply = models.Status.replies(status).first()
        self.assertEqual(reply.content, "<p>right</p>")
        self.assertEqual(reply.user, user)
        # the mentioned user in the parent post is only included if @'ed
        self.assertFalse(self.remote_user in reply.mention_users.all())
        self.assertTrue(self.local_user in reply.mention_users.all())

    def test_delete_and_redraft(self, *_):
        """delete and re-draft a status"""
        view = views.DeleteAndRedraft.as_view()
        request = self.factory.post("")
        request.user = self.local_user
        status = models.Comment.objects.create(
            content="hi", book=self.book, user=self.local_user
        )

        with patch("bookwyrm.activitystreams.remove_status_task.delay") as mock:
            result = view(request, status.id)
            self.assertTrue(mock.called)
        result.render()

        # make sure it was deleted
        status.refresh_from_db()
        self.assertTrue(status.deleted)

    def test_delete_and_redraft_invalid_status_type_rating(self, *_):
        """you can't redraft generated statuses"""
        view = views.DeleteAndRedraft.as_view()
        request = self.factory.post("")
        request.user = self.local_user
        with patch("bookwyrm.activitystreams.add_status_task.delay"):
            status = models.ReviewRating.objects.create(
                book=self.book, rating=2.0, user=self.local_user
            )

        with patch("bookwyrm.activitystreams.remove_status_task.delay") as mock:
            result = view(request, status.id)
            self.assertFalse(mock.called)
        self.assertEqual(result.status_code, 400)

        status.refresh_from_db()
        self.assertFalse(status.deleted)

    def test_delete_and_redraft_invalid_status_type_generated_note(self, *_):
        """you can't redraft generated statuses"""
        view = views.DeleteAndRedraft.as_view()
        request = self.factory.post("")
        request.user = self.local_user
        with patch("bookwyrm.activitystreams.add_status_task.delay"):
            status = models.GeneratedNote.objects.create(
                content="hi", user=self.local_user
            )

        with patch("bookwyrm.activitystreams.remove_status_task.delay") as mock:
            result = view(request, status.id)
            self.assertFalse(mock.called)
        self.assertEqual(result.status_code, 400)

        status.refresh_from_db()
        self.assertFalse(status.deleted)

    def test_find_mentions(self, *_):
        """detect and look up @ mentions of users"""
        user = models.User.objects.create_user(
            f"nutria@{DOMAIN}",
            "nutria@nutria.com",
            "password",
            local=True,
            localname="nutria",
        )
        self.assertEqual(user.username, f"nutria@{DOMAIN}")

        self.assertEqual(
            list(views.status.find_mentions("@nutria"))[0], ("@nutria", user)
        )
        self.assertEqual(
            list(views.status.find_mentions("leading text @nutria"))[0],
            ("@nutria", user),
        )
        self.assertEqual(
            list(views.status.find_mentions("leading @nutria trailing text"))[0],
            ("@nutria", user),
        )
        self.assertEqual(
            list(views.status.find_mentions("@rat@example.com"))[0],
            ("@rat@example.com", self.remote_user),
        )

        multiple = list(views.status.find_mentions("@nutria and @rat@example.com"))
        self.assertEqual(multiple[0], ("@nutria", user))
        self.assertEqual(multiple[1], ("@rat@example.com", self.remote_user))

        with patch("bookwyrm.views.status.handle_remote_webfinger") as rw:
            rw.return_value = self.local_user
            self.assertEqual(
                list(views.status.find_mentions("@beep@beep.com"))[0],
                ("@beep@beep.com", self.local_user),
            )
        with patch("bookwyrm.views.status.handle_remote_webfinger") as rw:
            rw.return_value = None
            self.assertEqual(list(views.status.find_mentions("@beep@beep.com")), [])

        self.assertEqual(
            list(views.status.find_mentions(f"@nutria@{DOMAIN}"))[0],
            (f"@nutria@{DOMAIN}", user),
        )

    def test_format_links_simple_url(self, *_):
        """find and format urls into a tags"""
        url = "http://www.fish.com/"
        self.assertEqual(
            views.status.format_links(url), f'<a href="{url}">www.fish.com/</a>'
        )
        self.assertEqual(
            views.status.format_links(f"({url})"),
            f'(<a href="{url}">www.fish.com/</a>)',
        )

    def test_format_links_paragraph_break(self, *_):
        """find and format urls into a tags"""
        url = """okay

http://www.fish.com/"""
        self.assertEqual(
            views.status.format_links(url),
            'okay\n\n<a href="http://www.fish.com/">www.fish.com/</a>',
        )

    def test_format_links_parens(self, *_):
        """find and format urls into a tags"""
        url = "http://www.fish.com/"
        self.assertEqual(
            views.status.format_links(f"({url})"),
            f'(<a href="{url}">www.fish.com/</a>)',
        )

    def test_format_links_special_chars(self, *_):
        """find and format urls into a tags"""
        url = "https://archive.org/details/dli.granth.72113/page/n25/mode/2up"
        self.assertEqual(
            views.status.format_links(url),
            f'<a href="{url}">'
            "archive.org/details/dli.granth.72113/page/n25/mode/2up</a>",
        )
        url = "https://openlibrary.org/search?q=arkady+strugatsky&mode=everything"
        self.assertEqual(
            views.status.format_links(url),
            f'<a href="{url}">openlibrary.org/search'
            "?q=arkady+strugatsky&mode=everything</a>",
        )
        url = "https://tech.lgbt/@bookwyrm"
        self.assertEqual(
            views.status.format_links(url), f'<a href="{url}">tech.lgbt/@bookwyrm</a>'
        )
        url = "https://users.speakeasy.net/~lion/nb/book.pdf"
        self.assertEqual(
            views.status.format_links(url),
            f'<a href="{url}">users.speakeasy.net/~lion/nb/book.pdf</a>',
        )
        url = "https://pkm.one/#/page/The%20Book%20launched%20a%201000%20Note%20apps"
        self.assertEqual(
            views.status.format_links(url), f'<a href="{url}">{url[8:]}</a>'
        )

    def test_to_markdown(self, *_):
        """this is mostly handled in other places, but nonetheless"""
        text = "_hi_ and http://fish.com is <marquee>rad</marquee>"
        result = views.status.to_markdown(text)
        self.assertEqual(
            result,
            '<p><em>hi</em> and <a href="http://fish.com">fish.com</a> ' "is rad</p>",
        )

    def test_to_markdown_detect_url(self, *_):
        """this is mostly handled in other places, but nonetheless"""
        text = "http://fish.com/@hello#okay"
        result = views.status.to_markdown(text)
        self.assertEqual(
            result,
            '<p><a href="http://fish.com/@hello#okay">fish.com/@hello#okay</a></p>',
        )

    def test_to_markdown_link(self, *_):
        """this is mostly handled in other places, but nonetheless"""
        text = "[hi](http://fish.com) is <marquee>rad</marquee>"
        result = views.status.to_markdown(text)
        self.assertEqual(result, '<p><a href="http://fish.com">hi</a> ' "is rad</p>")

    def test_handle_delete_status(self, mock, *_):
        """marks a status as deleted"""
        view = views.DeleteStatus.as_view()
        with patch("bookwyrm.activitystreams.add_status_task.delay"):
            status = models.Status.objects.create(user=self.local_user, content="hi")
        self.assertFalse(status.deleted)
        request = self.factory.post("")
        request.user = self.local_user

        with patch("bookwyrm.activitystreams.remove_status_task.delay") as redis_mock:
            view(request, status.id)
            self.assertTrue(redis_mock.called)
        activity = json.loads(mock.call_args_list[1][0][1])
        self.assertEqual(activity["type"], "Delete")
        self.assertEqual(activity["object"]["type"], "Tombstone")
        status.refresh_from_db()
        self.assertTrue(status.deleted)

    def test_handle_delete_status_permission_denied(self, *_):
        """marks a status as deleted"""
        view = views.DeleteStatus.as_view()
        with patch("bookwyrm.activitystreams.add_status_task.delay"):
            status = models.Status.objects.create(user=self.local_user, content="hi")
        self.assertFalse(status.deleted)
        request = self.factory.post("")
        request.user = self.remote_user

        view(request, status.id)

        status.refresh_from_db()
        self.assertFalse(status.deleted)

    def test_handle_delete_status_moderator(self, mock, *_):
        """marks a status as deleted"""
        view = views.DeleteStatus.as_view()
        with patch("bookwyrm.activitystreams.add_status_task.delay"):
            status = models.Status.objects.create(user=self.local_user, content="hi")
        self.assertFalse(status.deleted)
        request = self.factory.post("")
        request.user = self.remote_user
        request.user.is_superuser = True

        with patch("bookwyrm.activitystreams.remove_status_task.delay") as redis_mock:
            view(request, status.id)
            self.assertTrue(redis_mock.called)
        activity = json.loads(mock.call_args_list[1][0][1])
        self.assertEqual(activity["type"], "Delete")
        self.assertEqual(activity["object"]["type"], "Tombstone")
        status.refresh_from_db()
        self.assertTrue(status.deleted)
