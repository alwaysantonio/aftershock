from datetime import date


from rest_framework import serializers

from .models import IngestWindow

MAX_BACKFILL_DAYS = 366 * 10


class IngestWindowSerializer(serializers.ModelSerializer):
    class Meta:
        model = IngestWindow
        fields = [
            "id",
            "start",
            "end",
            "status",
            "attempts",
            "fetched",
            "inserted",
            "updated_count",
            "rejected",
            "last_error",
            "created_at",
            "started_at",
            "finished_at",
        ]
        read_only_fields = fields


class BackfillRequestSerializer(serializers.Serializer):
    start = serializers.DateField()
    end = serializers.DateField(help_text="Exclusive")

    def validate(self, data):
        if data["end"] <= data["start"]:
            raise serializers.ValidationError({"end": "must be after start"})
        if (data["end"] - data["start"]).days > MAX_BACKFILL_DAYS:
            raise serializers.ValidationError(
                {"end": f"range is limited to {MAX_BACKFILL_DAYS} days per request"}
            )
        if data["start"] < date(1900, 1, 1):
            raise serializers.ValidationError({"start": "must be 1900-01-01 or later"})
        return data
