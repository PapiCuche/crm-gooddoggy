from django.urls import path

from apps.contacts.api import views

urlpatterns = [
    path("contacts/", views.ContactsView.as_view(), name="contacts"),
    path("contacts/<uuid:contact_id>/", views.ContactView.as_view(), name="contact"),
    path(
        "contacts/<uuid:contact_id>/assignment/",
        views.ContactAssignmentView.as_view(),
        name="contact-assignment",
    ),
]
