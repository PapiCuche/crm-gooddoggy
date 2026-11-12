from django.urls import path

from apps.quotes.api import quotes

urlpatterns = [
    path("quotes/", quotes.QuotesView.as_view(), name="quotes"),
    path("quotes/<uuid:quote_id>/", quotes.QuoteView.as_view(), name="quote"),
]
