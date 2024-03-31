# Generated by Django 4.1.13 on 2024-03-28 20:28

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("language_model", "0048_alter_autogeneratedtitle_embedding_and_more"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="ragconfig",
            name="index_up_to_date",
        ),
        migrations.AddField(
            model_name="ragconfig",
            name="index_status",
            field=models.CharField(
                choices=[
                    ("no_index", "No Index"),
                    ("outdated", "Outdated"),
                    ("up_to_date", "Up to Date"),
                ],
                default="no_index",
                editable=False,
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="llmconfig",
            name="llm_type",
            field=models.CharField(
                choices=[
                    ("vllm", "VLLM Client"),
                    ("openai", "OpenAI Model"),
                    ("claude", "Claude Model"),
                    ("mistral", "Mistral Model"),
                ],
                default="openai",
                max_length=10,
            ),
        ),
    ]