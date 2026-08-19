from django.urls import path
from . import views

app_name = 'activities'

urlpatterns = [
    path('', views.activity_list, name='activity_list'),
    path('<int:activity_id>/', views.activity_detail, name='activity_detail'),
]
