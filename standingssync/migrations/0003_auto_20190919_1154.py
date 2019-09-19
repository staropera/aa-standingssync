# Generated by Django 2.2.5 on 2019-09-19 11:54

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('standingssync', '0002_auto_20190919_1056'),
    ]

    operations = [
        migrations.AlterField(
            model_name='syncedcharacter',
            name='last_error',
            field=models.IntegerField(choices=[(0, 'No error'), (1, 'Invalid token'), (2, 'Expired token'), (3, 'Insufficient permissions'), (5, 'ESI API is currently unavailable'), (99, 'Unknown error')], default=0),
        ),
        migrations.AlterField(
            model_name='syncmanager',
            name='last_error',
            field=models.IntegerField(choices=[(0, 'No error'), (1, 'Invalid token'), (2, 'Expired token'), (3, 'Insufficient permissions'), (4, 'No character set for fetching alliance contacts'), (5, 'ESI API is currently unavailable'), (99, 'Unknown error')], default=0),
        ),
    ]
