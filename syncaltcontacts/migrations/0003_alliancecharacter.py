# Generated by Django 2.2.5 on 2019-09-16 12:33

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('authentication', '0016_ownershiprecord'),
        ('syncaltcontacts', '0002_auto_20190916_1100'),
    ]

    operations = [
        migrations.CreateModel(
            name='AllianceCharacter',
            fields=[
                ('character', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, primary_key=True, serialize=False, to='authentication.CharacterOwnership')),
            ],
        ),
    ]
