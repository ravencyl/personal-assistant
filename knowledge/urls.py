from django.urls import path
from . import views

app_name = 'knowledge'

urlpatterns = [
    path('', views.article_list, name='article_list'),
    path('create/', views.article_create, name='article_create'),
    path('<str:slug>/', views.article_detail, name='article_detail'),
    path('<int:pk>/edit/', views.article_edit, name='article_edit'),
    path('<int:pk>/delete/', views.article_delete, name='article_delete'),
]
