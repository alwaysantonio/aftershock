from rest_framework import serializers

from .models import Earthquake, EarthquakeRevision

USGS_EVENT_PAGE = "https://earthquake.usgs.gov/earthquakes/eventpage/{}"


class EarthquakeSerializer(serializers.ModelSerializer):
    id = serializers.CharField(source="usgs_id", read_only=True)
    usgs_url = serializers.SerializerMethodField()

    class Meta:
        model = Earthquake
        fields = [
            "id",
            "time",
            "updated",
            "mag",
            "mag_type",
            "latitude",
            "longitude",
            "depth_km",
            "place",
            "status",
            "event_type",
            "tsunami",
            "sig",
            "usgs_url",
        ]
        read_only_fields = fields

    def get_usgs_url(self, obj) -> str:
        return USGS_EVENT_PAGE.format(obj.usgs_id)


class RevisionSerializer(serializers.ModelSerializer):
    class Meta:
        model = EarthquakeRevision
        fields = [
            "updated",
            "received_at",
            "mag",
            "mag_type",
            "status",
            "latitude",
            "longitude",
            "depth_km",
        ]
        read_only_fields = fields


class EarthquakeDetailSerializer(EarthquakeSerializer):
    """Detail with ?expand=revisions: the newest versions, prefetched in one query."""

    revisions = RevisionSerializer(many=True, read_only=True, source="recent_revisions")

    class Meta(EarthquakeSerializer.Meta):
        fields = EarthquakeSerializer.Meta.fields + ["revisions"]
        read_only_fields = fields
