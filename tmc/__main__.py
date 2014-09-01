#!/usr/bin/env python3

import os
import sys
from base64 import b64encode
from functools import wraps
from getpass import getpass
from subprocess import Popen

import peewee

import argh
from argh.decorators import aliases, arg
from tmc import api
from tmc.errors import (APIError, NoCourseSelected, NoExerciseSelected,
                        TMCError, TMCExit)
from tmc.exercise_tests.basetest import run_test
from tmc.files import download_exercise, submit_exercise
from tmc.models import Config, Course, Exercise, reset_db
from tmc.ui.menu import Menu
from tmc.ui.prompt import custom_prompt, yn_prompt
from tmc.ui.spinner import Spinner
from tmc.version import __version__

try:
    from subprocess import DEVNULL
except ImportError:
    DEVNULL = open(os.devnull, 'wb')

from tmc.coloring import as_success, as_error


def selected_course(func):
    """
    Passes the selected course as the first argument to func.
    """
    @wraps(func)
    def inner(*args, **kwargs):
        course = Course.get_selected()
        return func(course, *args, **kwargs)
    return inner


def selected_exercise(func):
    """
    Passes the selected exercise as the first argument to func.
    """
    @wraps(func)
    def inner(*args, **kwargs):
        exercise = Exercise.get_selected()
        return func(exercise, *args, **kwargs)
    return inner


def false_exit(func):
    """
    If func returns False the program exits immediately.
    """
    @wraps(func)
    def inner(*args, **kwargs):
        ret = func(*args, **kwargs)
        if ret is False:
            if "TMC_TESTING" in os.environ:
                raise TMCExit()
            else:
                sys.exit(-1)
        return ret
    return inner


@aliases("init", "conf")
@arg("-s", "--server", help="Server address to be used.")
@arg("-u", "--username", help="Username to be used.")
@arg("-p", "--password", help="Password to be used.")
@arg("-i", "--id", dest="tid", help="Course ID to be used.")
@arg("-a", "--auto", action="store_true", help="Don't prompt for download path, use default instead")
@false_exit
def configure(server=None, username=None, password=None, tid=None, auto=False):
    """
    Configure tmc.py to use your account.
    """
    if not server and not username and not password and not tid:
        if Config.has():
            sure = input("Override old configuration [y/N]: ")
            if sure.upper() != "Y":
                return False
    reset_db()
    if not server:
        while True:
            server = input("Server url [https://tmc.mooc.fi/mooc/]: ").strip()
            if len(server) == 0:
                server = "https://tmc.mooc.fi/mooc/"
            if not server.endswith('/'):
                server += '/'
            if not (server.startswith("http://")
                    or server.startswith("https://")):
                ret = custom_prompt(
                    "Server should start with http:// or https://\n" +
                    "R: Retry, H: Assume http://, S: Assume https://",
                    ["r", "h", "s"], "r")
                if ret == "r":
                    continue
                # Strip previous schema
                if "://" in server:
                    server = server.split("://")[1]
                if ret == "h":
                    server = "http://" + server
                elif ret == "s":
                    server = "https://" + server
            break

        print("Using URL: '{0}'".format(server))
    while True:
        if not username:
            username = input("Username: ")
        if not password:
            password = getpass("Password: ")
        # wow, such security
        token = b64encode(
            bytes("{0}:{1}".format(username, password), encoding='utf-8')
        ).decode("utf-8")

        try:
            api.configure(url=server, token=token, test=True)
        except APIError as e:
            print(e)
            if auto is False and yn_prompt("Retry authentication"):
                username = password = None
                continue
            return False
        break
    if tid:
        select(course=True, tid=tid, auto=auto)
    else:
        select(course=True)


@aliases("cur")
@selected_exercise
def current(exercise):
    """
    Prints some small info about the current exercise.
    """
    list_all(single=exercise)


@aliases("dl")
@arg("-i", "--id", dest="tid", help="Download this ID.")
@arg("-a", "--all", default=False, action="store_true",
     dest="dl_all", help="Download all exercises.")
@arg("-f", "--force", default=False, action="store_true",
     help="Should the download be forced.")
@arg("-u", "--upgrade", default=False, action="store_true",
     help="Should the Java target be upgraded from 1.6 to 1.7")
@selected_course
def download(course, tid=None, dl_all=False, force=False, upgrade=False):
    """
    Download the exercises from the server.
    """

    def dl(id):
        download_exercise(Exercise.get(Exercise.tid == id),
                          force=force,
                          update_java=upgrade)

    if dl_all:
        for exercise in list(course.exercises):
            dl(exercise.tid)
    elif tid is not None:
        dl(int(tid))
    else:
        for exercise in list(course.exercises):
            if not exercise.is_completed:
                dl(exercise.tid)
            else:
                exercise.update_downloaded()


@aliases("next")
@selected_course
@false_exit
def skip(course, num=1):
    """
    Go to the next exercise.
    """
    sel = None
    try:
        sel = Exercise.get_selected()
        if sel.course.tid != course.tid:
            sel = None
    except NoExerciseSelected:
        pass

    if sel is None:
        sel = course.exercises.first()
    else:
        try:
            sel = Exercise.get(Exercise.id == sel.id + num)
        except peewee.DoesNotExist:
            print("There are no more exercises in this course.")
            return False

    sel.set_select()
    list_all(single=sel)


@aliases("prev")
def previous():
    skip(num=-1)


@aliases("resetdb")
def reset():
    """
    Resets the local database.
    """
    print("This won't remove any of your files,",
          "but instead the local database that tracks your progress.")
    if yn_prompt("Reset database", False):
        reset_db()
        print("Database resetted. You will need to tmc configure again.")


@arg('command', help='The command')
@selected_exercise
def run(exercise, command):
    """
    Spawns a process with `command path-of-exercise`
    """
    Popen(['nohup', command, exercise.path()], stdout=DEVNULL, stderr=DEVNULL)


@aliases("sel")
@arg("-c", "--course", action="store_true", help="Select a course instead.")
@arg("-i", "--id", dest="tid",
     help="Select this ID without invoking the curses UI.")
def select(course=False, tid=None, auto=False):
    """
    Select a course or an exercise.
    """
    if course:
        update(course=True)
        course = None
        try:
            course = Course.get_selected()
        except NoCourseSelected:
            pass

        ret = {}
        if not tid:
            ret = Menu.launch("Select a course",
                              Course.select().execute(),
                              course)
        else:
            ret["item"] = Course.get(Course.tid == tid)
        if "item" in ret:
            ret["item"].set_select()
            update()
            if ret["item"].path == "":
                select_a_path(auto=auto)
            # Selects the first exercise in this course
            skip()
            return
        else:
            print("You can select the course with `tmc select --course`")
            return
    else:
        selected = None
        try:
            selected = Exercise.get_selected()
        except NoExerciseSelected:
            pass

        ret = {}
        if not tid:
            ret = Menu.launch("Select an exercise",
                              Course.get_selected().exercises,
                              selected)
        else:
            ret["item"] = Exercise.byid(tid)
        if "item" in ret:
            ret["item"].set_select()
            print("Selected {}".format(ret["item"]))


@aliases("su")
@arg("-i", "--id", dest="tid", help="Submit this ID.")
@arg("-p", "--pastebin", default=False, action="store_true",
     help="Should the submission be sent to TMC pastebin.")
@arg("-r", "--review", default=False, action="store_true",
     help="Request a review for this submission.")
@selected_course
@false_exit
def submit(course, tid=None, pastebin=False, review=False):
    """
    Submit the selected exercise to the server.
    """
    if tid is not None:
        return submit_exercise(Exercise.byid(tid),
                               pastebin=pastebin,
                               request_review=review)
    else:
        sel = Exercise.get_selected()
        if not sel:
            raise NoExerciseSelected()
        return submit_exercise(sel, pastebin=pastebin, request_review=review)


@aliases("te")
@arg("-i", "--id", dest="tid", help="Test this ID.")
@selected_course
@false_exit
def test(course, tid=None):
    """
    Run tests on the selected exercise.
    """
    if tid is not None:
        return run_test(Exercise.byid(tid))
    else:
        sel = Exercise.get_selected()
        if not sel:
            raise NoExerciseSelected()
        return run_test(sel)


@aliases("ls", "listall")
@selected_course
def list_all(course, single=None):
    """
    Lists all of the exercises in the current course.
    """

    def bs(val):
        return "●" if val else " "

    def bc(val):
        return as_success("✔") if val else as_error("✘")

    def format_line(exercise):
        return "{0} │ {1} │ {2} │ {3} │ {4}".format(exercise.tid,
                                                    bs(exercise.is_selected),
                                                    bc(exercise.is_downloaded),
                                                    bc(exercise.is_completed),
                                                    exercise.menuname())

    print("ID{0}│ S │ D │ C │ Name".format(
        (len(str(course.exercises[0].tid)) - 1) * " "
    ))
    if single:
        print(format_line(single))
        return
    for exercise in course.exercises:
        # ToDo: use a pager
        print(format_line(exercise))


@aliases("up")
@arg("-c", "--course", action="store_true", help="Update courses instead.")
def update(course=False):
    """
    Update the metadata of courses and or exercises from server.
    """
    if course:
        @Spinner.decorate("Updated course metadata.",
                          waitmsg="Updating course metadata.")
        def update_course():
            for course in api.get_courses():
                old = None
                try:
                    old = Course.get(Course.tid == course["id"])
                except peewee.DoesNotExist:
                    old = None
                if old:
                    continue
                Course.create(tid=course["id"],
                              name=course["name"])
        update_course()
    else:
        selected = Course.get_selected()

        @Spinner.decorate("Updated exercise metadata.",
                          waitmsg="Updating exercise metadata.")
        def update_exercise():
            for exercise in api.get_exercises(selected.tid):
                old = None
                try:
                    old = Exercise.byid(exercise["id"])
                except peewee.DoesNotExist:
                    old = None
                if old is not None:
                    old.name = exercise["name"]
                    old.course = selected.id
                    old.is_attempted = exercise["attempted"]
                    old.is_completed = exercise["completed"]
                    old.deadline = exercise.get("deadline")
                    old.is_downloaded = os.path.isdir(old.path())
                    old.save()
                else:
                    ex = Exercise.create(tid=exercise["id"],
                                         name=exercise["name"],
                                         course=selected.id,
                                         is_attempted=exercise["attempted"],
                                         is_completed=exercise["completed"],
                                         deadline=exercise.get("deadline"))
                    ex.is_downloaded = os.path.isdir(ex.path())
                    ex.save()
        update_exercise()


@selected_course
def select_a_path(course, auto=False):
    defpath = os.path.join(os.path.expanduser("~"),
                           "tmc",
                           course.name)
    if auto:
        path = defpath
    else:
        path = input("File download path [{0}]: ".format(defpath)).strip()
    if len(path) == 0:
        path = defpath
    path = os.path.expanduser(path)
    # I guess we are looking at a relative path
    if not path.startswith("/"):
        path = os.path.join(os.getcwd(), path)
    print("Using path: '{}'".format(path))
    course.path = path
    course.save()
    if auto:
        return
    ret = custom_prompt("Download exercises R: Remaining A: All N: None",
                        ["r", "a", "n"],
                        "r")
    if ret == "a":
        download(dl_all=True)
    elif ret == "r":
        download()
    else:
        print("You can download the exercises with `tmc download --all`")


def version():
    """
    Prints the version and exits.
    """
    print("tmc.py version {0}".format(__version__))
    print("Copyright 2014 tmc.py contributors")

commands = [select, update, download, test, submit, skip, current, previous,
            reset, configure, version, list_all, run]


def main():
    parser = argh.ArghParser()
    parser.add_commands(commands)
    try:
        parser.dispatch()
    except TMCError as e:
        print(e)
        exit(-1)


def run_command(argv):
    from io import StringIO
    if type(argv) == str:
        argv = [argv]
    parser = argh.ArghParser()
    parser.add_commands(commands)
    sys.stdout = StringIO()
    sys.stderr = StringIO()
    exception = None
    try:
        parser.dispatch(argv=argv)
    except Exception as e:
        exception = e
    return sys.stdout.getvalue(), sys.stderr.getvalue(), exception


if __name__ == "__main__":
    main()
