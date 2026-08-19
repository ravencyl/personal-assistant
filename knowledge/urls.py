from django.urls import path
from . import views

app_name = 'knowledge'

urlpatterns = [
    path('', views.index, name='index'),
    path('article/<int:pk>/', views.article_detail, name='article_detail'),
    path('ai-ask/', views.ai_ask, name='ai_ask'),
]
