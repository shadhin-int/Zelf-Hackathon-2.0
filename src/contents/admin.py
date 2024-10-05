from django.contrib import admin
from .models import Content

# Register your models here.
@admin.register(Content)
class ContentAdmin(admin.ModelAdmin):
	list_display = ("id", "title")
	list_display_links = ("id", "title")
	search_fields = ("title", "content")
	list_per_page = 25