from django.db import models
from django.contrib.auth import get_user_model


class Experience(models.Model):
    order = models.IntegerField(default=0)  # Добавлено для сортировки
    user = models.ForeignKey(get_user_model(), on_delete=models.CASCADE)
    job_title = models.CharField(max_length=100)
    company = models.CharField(max_length=100)
    url = models.URLField(null=True, blank=True)
    city = models.CharField(max_length=100)
    country = models.CharField(max_length=100)
    start_date = models.CharField(max_length=100)
    end_date = models.CharField(max_length=100, null=True, blank=True)
    description = models.TextField()

    def __str__(self):
        return self.job_title

    class Meta:
        ordering = ['id']
