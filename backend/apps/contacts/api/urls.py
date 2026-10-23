from django.urls import path

from apps.contacts.api import notes, tags, views

urlpatterns = [
    path("contacts/", views.ContactsView.as_view(), name="contacts"),
    path("contacts/<uuid:contact_id>/", views.ContactView.as_view(), name="contact"),
    path(
        "contacts/<uuid:contact_id>/assignment/",
        views.ContactAssignmentView.as_view(),
        name="contact-assignment",
    ),
    path("contacts/<uuid:contact_id>/notes/", notes.NotesView.as_view(), name="notes"),
    path("contacts/<uuid:contact_id>/notes/<uuid:note_id>/", notes.NoteView.as_view(), name="note"),
    path("tags/", tags.TagsView.as_view(), name="tags"),
    path("tags/<uuid:tag_id>/", tags.TagView.as_view(), name="tag"),
]
