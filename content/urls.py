from django.urls import path
from . import views

app_name = 'content'

urlpatterns = [
    path('', views.bookmark_list, name='bookmark_list'),
    path('create/', views.create_bookmark, name='create_bookmark'),
    path('<int:pk>/', views.bookmark_detail, name='bookmark_detail'),
    path('<int:pk>/delete/', views.delete_bookmark, name='delete_bookmark'),
    path('<int:pk>/ai-summary/', views.generate_ai_summary, name='generate_ai_summary'),
    path('feeds/', views.feed_list, name='feed_list'),
]
