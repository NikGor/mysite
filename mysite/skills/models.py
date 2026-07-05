from django.db import models
from mysite.user.models import User


class Skill(models.Model):
    order = models.IntegerField(default=0)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    skill_type = models.CharField(max_length=100)
    description = models.TextField()

    class Meta:
        ordering = ['order']
