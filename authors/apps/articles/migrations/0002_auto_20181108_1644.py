# Generated by Django 2.1.2 on 2018-11-08 16:44

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('articles', '0001_initial'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='article',
            options={'ordering': ['-updated_at']},
        ),
    ]
