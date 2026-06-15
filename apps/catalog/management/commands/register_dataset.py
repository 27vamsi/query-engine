from django.core.management.base import BaseCommand, CommandError

from apps.catalog.services import register_dataset


class Command(BaseCommand):
    help = "Register (or refresh) a dataset by introspecting its Parquet files."

    def add_arguments(self, parser):
        parser.add_argument("name")
        parser.add_argument("uri")
        parser.add_argument("--description", default="")

    def handle(self, *args, **opts):
        try:
            ds = register_dataset(
                name=opts["name"], uri=opts["uri"], description=opts["description"]
            )
        except (ValueError, FileNotFoundError) as e:
            raise CommandError(str(e)) from e
        self.stdout.write(
            self.style.SUCCESS(
                f"Registered '{ds.name}': {ds.file_count} files, "
                f"{len(ds.column_names)} columns "
                f"(partitions: {', '.join(ds.partition_columns) or 'none'})"
            )
        )
