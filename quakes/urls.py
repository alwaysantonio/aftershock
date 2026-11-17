from rest_framework.routers import SimpleRouter

from .views import EarthquakeViewSet

router = SimpleRouter()
router.register("earthquakes", EarthquakeViewSet, basename="earthquake")

urlpatterns = router.urls
