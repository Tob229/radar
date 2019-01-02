import logging
import json

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.urls import reverse
from django.http.response import JsonResponse, HttpResponse
from django.shortcuts import render, redirect, get_object_or_404

from provider import aplus
from data.models import Course, Comparison
from data import graph
from radar.config import provider_config, configured_function
from review.decorators import access_resource
from review.forms import ExerciseForm, ExerciseTemplateForm


logger = logging.getLogger("radar.review")

class APIAuthException(BaseException): pass

@login_required
def index(request):
    return render(request, "review/index.html", {
        "hierarchy": ((settings.APP_NAME, None),),
        "courses": Course.objects.get_available_courses(request.user)
    })


@access_resource
def course(request, course_key=None, course=None):
    return render(request, "review/course.html", {
        "hierarchy": ((settings.APP_NAME, reverse("index")), (course.name, None)),
        "course": course,
        "exercises": course.exercises.all(),
        "num_exercises_with_unassigned_submissions": sum(e.has_unassigned_submissions for e in course.exercises.all())
    })


@access_resource
def course_histograms(request, course_key=None, course=None):
    return render(request, "review/course_histograms.html", {
        "hierarchy": ((settings.APP_NAME, reverse("index")),
                      (course.name, reverse("course", kwargs={ "course_key": course.key })),
                      ("Histograms", None)),
        "course": course,
        "exercises": course.exercises.all(),
    })


@access_resource
def exercise(request, course_key=None, exercise_key=None, course=None, exercise=None):
    return render(request, "review/exercise.html", {
        "hierarchy": ((settings.APP_NAME, reverse("index")),
                      (course.name, reverse("course", kwargs={ "course_key": course.key })),
                      (exercise.name, None)),
        "course": course,
        "exercise": exercise,
        "comparisons": exercise.top_comparisons(),
    })


@access_resource
def comparison(request, course_key=None, exercise_key=None, ak=None, bk=None, ck=None, course=None, exercise=None):
    comparison = get_object_or_404(Comparison, submission_a__exercise=exercise, pk=ck,
                                   submission_a__student__key=ak, submission_b__student__key=bk)
    if request.method == "POST":
        result = "review" in request.POST and comparison.update_review(request.POST["review"])
        if request.is_ajax():
            return JsonResponse({ "success": result })

    reverse_flag = False
    a = comparison.submission_a
    b = comparison.submission_b
    if "reverse" in request.GET:
        reverse_flag = True
        a = comparison.submission_b
        b = comparison.submission_a

    p_config = provider_config(course.provider)
    get_submission_text = configured_function(p_config, "get_submission_text")
    return render(request, "review/comparison.html", {
        "hierarchy": ((settings.APP_NAME, reverse("index")),
                      (course.name, reverse("course", kwargs={ "course_key": course.key })),
                      (exercise.name, reverse("exercise",
                                              kwargs={ "course_key": course.key, "exercise_key": exercise.key })),
                      ("%s vs %s" % (a.student.key, b.student.key), None)),
        "course": course,
        "exercise": exercise,
        "comparisons": exercise.comparisons_for_student(a.student),
        "comparison": comparison,
        "reverse": reverse_flag,
        "a": a,
        "b": b,
        "source_a": get_submission_text(a, p_config),
        "source_b": get_submission_text(b, p_config)
    })


@access_resource
def marked_submissions(request, course_key=None, course=None):
    comparisons = Comparison.objects\
        .filter(submission_a__exercise__course=course, review__gte=5)\
        .order_by("submission_a__created")\
        .select_related("submission_a", "submission_b","submission_a__exercise", "submission_a__student", "submission_b__student")
    suspects = {}
    for c in comparisons:
        for s in (c.submission_a.student, c.submission_b.student):
            if s.id not in suspects:
                suspects[s.id] = { 'key':s.key, 'sum':0, 'comparisons':[] }
            suspects[s.id]['sum'] += c.review
            suspects[s.id]['comparisons'].append(c)
    return render(request, "review/marked.html", {
        "hierarchy": ((settings.APP_NAME, reverse("index")),
                      (course.name, reverse("course", kwargs={ "course_key": course.key })),
                      ("Marked submissions", None)),
        "course": course,
        "suspects": sorted(suspects.values(), reverse=True, key=lambda e: e['sum']),
    })


def leafs_with_radar_config(exercises):
    """
    Return an iterator yielding dictionaries of leaf exercises that have Radar configurations.
    """
    if not exercises:
        return
    for exercise in exercises:
        child_exercises = exercise.get("exercises")
        if child_exercises:
            yield from leafs_with_radar_config(child_exercises)
        else:
            radar_config = aplus.get_radar_config(exercise)
            if radar_config:
                yield radar_config


def submittable_exercises(exercises):
    """
    Return an iterator yielding dictionaries of leaf exercises that are submittable.
    """
    if not exercises:
        return
    for exercise in exercises:
        child_exercises = exercise.get("exercises")
        if child_exercises:
            yield from submittable_exercises(child_exercises)
        elif "is_submittable" in exercise and exercise["is_submittable"]:
            # Insert a radar config into the exercise api dict, while avoiding to overwrite exercise_info data
            patched_exercise_info = dict(exercise["exercise_info"] or {}, radar={"tokenizer": "skip", "minimum_match_tokens": 15})
            exercise.add_data({"exercise_info": patched_exercise_info})
            radar_config = aplus.get_radar_config(exercise)
            if radar_config:
                yield radar_config


@access_resource
def configure_course(request, course_key=None, course=None):
    context = {
        "hierarchy": ((settings.APP_NAME, reverse("index")),
                      (course.name, reverse("course", kwargs={ "course_key": course.key })),
                      ("Configure", None)),
        "course": course,
        "provider_data": [
            {
                "name": 'Submission hook',
                "description": 'Data providers should make POST-requests, containing submission IDs, to this path',
                "path": reverse("hook_submission", kwargs={"course_key": course.key}),
            },
            {
                "name": 'LTI login',
                "description": 'Login requests using the LTI-protocol should be made to this path',
                "path": reverse("lti_login"),
            },
        ],
        "errors": []
    }
    p_config = provider_config(course.provider)
    # an HTML POST request + template rendering abomination
    if "provider-fetch-automatic" in request.POST or "provider-fetch-manual" in request.POST:
        client = request.user.get_api_client(course.namespace)
        try:
            if client is None:
                raise APIAuthException
            response = client.load_data(course.url)
            if response is None:
                raise APIAuthException
            exercises = response.get("exercises", [])
        except APIAuthException:
            exercises = []
            context["errors"].append("This user does not have correct credentials to use the API of %s" % repr(course))
        if not exercises:
            context["errors"].append("No exercises found for %s" % repr(course))
        elif "provider-fetch-automatic" in request.POST:
            # Exercise API data is expected to contain Radar configurations
            # Partition all radar configs into unseen and existing exercises
            new_exercises, old_exercises = [], []
            for radar_config in leafs_with_radar_config(exercises):
                radar_config["template_source"] = radar_config["get_template_source"]()
                # No need for the template source getter anymore
                del radar_config["get_template_source"]
                if course.has_exercise(radar_config["exercise_key"]):
                    old_exercises.append(radar_config)
                else:
                    new_exercises.append(radar_config)
            context["exercises"] = {
                "old": old_exercises,
                "new": new_exercises,
                "new_json": json.dumps(new_exercises),
            }
        elif "provider-fetch-manual" in request.POST:
            # Exercise API data is not expected to contain Radar data, choose all submittable exercises
            exercises_data = list(submittable_exercises(exercises))
            context["exercises"] = {
                "manual_config": True,
                "new": exercises_data,
                "tokenizer_choices": settings.TOKENIZER_CHOICES
            }
    elif "create_exercises" in request.POST or "overwrite_exercises" in request.POST:
        if "create_exercises" in request.POST:
            exercises = json.loads(request.POST["exercises_json"])
        elif "overwrite_exercises" in request.POST:
            checked = (key.split("-", 1)[0] for key in request.POST if key.endswith("enabled"))
            exercises = (
                {"exercise_key": exercise_key,
                 "name": request.POST[exercise_key + "-name"],
                 "tokenizer": request.POST[exercise_key + "-tokenizer"],
                 "minimum_match_tokens": request.POST[exercise_key + "-min-match-tokens"]}
                for exercise_key in checked
            )
        # TODO gather list of invalid exercise data and render as errors
        for exercise_data in exercises:
            # Create an exercise instance into the database
            key_str = str(exercise_data["exercise_key"])
            exercise = course.get_exercise(key_str)
            exercise.set_from_config(exercise_data)
            exercise.save()
            # Queue fetching all submissions for this exercise
            full_reload = configured_function(p_config, "full_reload")
            full_reload(exercise, p_config)
        context["create_exercises_success"] = True
    return render(request, "review/configure.html", context)


@access_resource
def graph_ui(request, course, course_key):
    """Course graph UI without the graph data."""
    context = {
        "hierarchy": (
            (settings.APP_NAME, reverse("index")),
            (course.name, reverse("course", kwargs={ "course_key": course.key })),
            ("Graph", None)
        ),
        "course": course,
    }
    return render(request, "review/graph.html", context)


@access_resource
def build_graph(request, course, course_key):
    if not request.POST or "minSimilarity" not in request.POST:
        return HttpResponse("Graph build arguments must contain 'minSimilarity' in the body of a POST request.", status=400)
    min_similarity = request.POST["minSimilarity"]
    graph_data = graph.generate_match_graph(course.key, min_similarity)
    return JsonResponse(graph_data)


@access_resource
def invalidate_graph_cache(request, course, course_key):
    graph.invalidate_course_graphs(course)
    return HttpResponse("Graph cache invalidated")


@access_resource
def exercise_settings(request, course_key=None, exercise_key=None, course=None, exercise=None):
    p_config = provider_config(course.provider)
    context = {
        "hierarchy": (
            (settings.APP_NAME, reverse("index")),
            (course.name, reverse("course", kwargs={ "course_key": course.key })),
            ("%s settings" % (exercise.name), None)
        ),
        "course": course,
        "exercise": exercise,
        "provider_reload": "full_reload" in p_config,
        "change_success": set(),
    }
    if request.method == "POST":
        if "save" in request.POST:
            form = ExerciseForm(request.POST)
            if form.is_valid():
                form.save(exercise)
                context["change_success"].add("save")
        elif "override_template" in request.POST:
            form_template = ExerciseTemplateForm(request.POST)
            if form_template.is_valid():
                form_template.save(exercise, request.POST.get("template_source"))
                context["change_success"].add("override_template")
        elif "clear_and_recompare" in request.POST:
            configured_function(p_config, "recompare")(exercise, p_config)
            context["change_success"].add("clear_and_recompare")
        elif "provider_reload" in request.POST:
            configured_function(p_config, "full_reload")(exercise, p_config)
            context["change_success"].add("provider_reload")
        elif "delete_exercise" in request.POST:
            exercise.delete()
            return redirect("course", course_key=course.key)
    template_source = aplus.load_exercise_template(exercise, p_config)
    if exercise.template_tokens and not template_source:
        context["template_source_error"] = True
        context["template_tokens"] = exercise.template_tokens
        context["template_source"] = ''
    else:
        context["template_source"] = template_source
    context["form"] = ExerciseForm({
        "name": exercise.name,
        "paused": exercise.paused,
        "tokenizer": exercise.tokenizer,
        "minimum_match_tokens": exercise.minimum_match_tokens,
    })
    context["form_template"] = ExerciseTemplateForm({
        "template": template_source,
    })
    return render(request, "review/exercise_settings.html", context)
