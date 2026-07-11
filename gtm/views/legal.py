from django.shortcuts import render


def privacy_policy(request):
    return render(request, "gtm/privacy.html")


def terms_of_service(request):
    return render(request, "gtm/terms.html")


def contact(request):
    return render(request, "gtm/contact.html")
