from django.urls import path
from . import views

app_name = 'knowledge'

urlpatterns = [
    path('', views.index, name='index'),
    path('new/', views.article_create, name='article_create'),
    path('article/<int:pk>/', views.article_detail, name='article_detail'),
    path('article/<int:pk>/edit/', views.article_edit, name='article_edit'),
    path('article/<int:pk>/delete/', views.article_delete, name='article_delete'),
    path('ai-ask/', views.ai_ask, name='ai_ask'),
]
