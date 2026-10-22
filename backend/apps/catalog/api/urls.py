from django.urls import path

from apps.catalog.api import views

urlpatterns = [
    path("brands/", views.BrandsView.as_view(), name="brands"),
    path("brands/<uuid:brand_id>/", views.BrandView.as_view(), name="brand"),
    path("categories/", views.CategoriesView.as_view(), name="categories"),
    path("categories/<uuid:category_id>/", views.CategoryView.as_view(), name="category"),
]
