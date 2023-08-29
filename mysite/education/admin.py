from django.contrib import admin
from .models import Education


@admin.register(Education)
class EducationAdmin(admin.ModelAdmin):
    list_display = ('school', 'faculty', 'url', 'start_date', 'end_date')
    list_editable = ('start_date', 'end_date', 'url')
    search_fields = ('school', 'faculty')
    ordering = ('-start_date',)

    fieldsets = (
        (None, {'fields': ('user',)}),
        ('Education Information', {'fields': ('school', 'faculty', 'url', 'description')}),
        ('Date', {'fields': ('start_date', 'end_date')}),
    )
