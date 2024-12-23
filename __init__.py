
import requests
import re
import json
import os

from CTFd.utils.scores import get_team_standings
from flask import request
from CTFd.plugins.challenges import CHALLENGE_CLASSES, BaseChallenge
from flask.wrappers import Response
from CTFd.utils.dates import ctftime
from CTFd.utils import config as ctfd_config
from CTFd.api.v1.submissions import Submission
from CTFd.utils.user import get_current_team, get_current_user
from CTFd.models import Challenges, Solves, Awards, Users, Teams, db, Submissions
from functools import wraps
from sqlalchemy import asc


from flask import (
    render_template,
    jsonify,
    Blueprint,
    url_for,
    session,
    redirect,
    request
)
from sqlalchemy.sql import or_

from CTFd import utils, scoreboard
from CTFd.models import db, Solves, Challenges
from CTFd.plugins import override_template
from CTFd.utils.config import is_scoreboard_frozen, ctf_theme, is_users_mode
from CTFd.utils.config.visibility import challenges_visible, scores_visible
from CTFd.utils.dates import (
    ctf_started, ctftime, view_after_ctf, unix_time_to_utc
)
from CTFd.models import db
from CTFd.utils.user import is_admin, authed
from sqlalchemy.dialects.postgresql import JSON  # Use this if PostgreSQL


sanreg = re.compile(r'(~|!|@|#|\$|%|\^|&|\*|\(|\)|\_|\+|\`|-|=|\[|\]|;|\'|,|\.|\/|\{|\}|\||:|"|<|>|\?)')
sanitize = lambda m: sanreg.sub(r"\1", m)

class FirstBloods(db.Model):
    team_id = db.Column(db.Integer, primary_key=True)
    count = db.Column(db.Integer)
    challenges = db.Column(db.JSON)

    def __init__(self, team_id, count, challenges):
        self.team_id = team_id
        self.count = count
        self.challenges = challenges

    @classmethod
    def addBlood(cls,blood_team_id,challenge):
        # check if already added to firstbloods table
        firstblood = cls.query.filter_by(team_id=blood_team_id).first()
        if firstblood:
            firstblood.count += 1
            print("[+] ### Adding challenge to bloods ... ### [+]")
            challs = list(firstblood.challenges)
            challs.append({"id":challenge.id})
            firstblood.challenges = challs
            db.session.commit()

    @classmethod    
    def InitiateCounts(cls):
        print("[+] ### Initiating blood counts... ### [+]")
        # delete old if exists
        db.session.query(FirstBloods).delete()        
        # add all teams with count 0 at first
        teams =Teams.query.all()
        for team in teams:
            team_init_first_blood = FirstBloods(team_id=team.id, count=0,challenges={})
            db.session.add(team_init_first_blood)          
            db.session.commit()            
        # get solves for all challenges
        challenges = Challenges.query.all()
        for chal in challenges:
            first_solve = Solves.query.filter_by(challenge_id=chal.id).order_by(asc(Solves.date)).first()
            if first_solve:
                cls.addBlood(first_solve.account_id,chal)     
            else:
                pass

        db.session.commit()
        print("[+] ### Done initiating bloods! ### [+]")

def get_team_bloods(blood_team_id):
    firstbloods = FirstBloods.query.filter_by(team_id=blood_team_id).first()
    return firstbloods

def load(app):
    app.db.create_all()
    TEAMS_MODE = ctfd_config.is_teams_mode()
    FirstBloods.InitiateCounts()    
    dir_path = os.path.dirname(os.path.realpath(__file__))
    template_path = os.path.join(dir_path, 'templates/scoreboard-bloods.html')
    override_template('scoreboard.html', open(template_path).read())

    def get_standings():
        standings = scoreboard.get_standings()
        new_standings = []
        for team in standings:
            teamid = team[0]
            bloods = get_team_bloods(teamid).count
            new_standings.append({'account_id': team[0], 'score': team[5], 'name': team[2], 'bloods_count':bloods})
        db.session.close()
        return new_standings

    def scoreboard_view():
        if scores_visible() and not authed():
            return redirect(url_for('auth.login', next=request.path))
        if not scores_visible():
            return render_template('scoreboard.html',
                                   errors=['Scores are currently hidden'])
        standings = get_standings()
        return render_template('scoreboard.html', standings=standings,
                               score_frozen=is_scoreboard_frozen(),
                               mode='users' if is_users_mode() else 'teams', 
                               theme=ctf_theme())


    def challenge_attempt_decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            result = f(*args, **kwargs)
            if not ctftime():
                return result
            if isinstance(result, Response):
                data = result.json
                if (isinstance(data, dict) and data.get("success") == True and isinstance(data.get("data"), dict)and data.get("data").get("status") == "correct"):
                    if request.content_type != "application/json":
                        request_data = request.form
                    else:
                        request_data = request.get_json()

                    first_blood = 0 
                    challenge_id = request_data.get("challenge_id")
                    challenge = Challenges.query.filter_by(id=challenge_id).first_or_404()
                    # get all solves for that challenge
                    solvers = Solves.query.filter_by(challenge_id=challenge.id)
                    
                    if TEAMS_MODE: 
                        solvers = solvers.filter(Solves.team.has(hidden=False))
                    
                    # if solve count for the challenge is 1 => firstblooded
                    # check if first blood
                    num_solves_chall = solvers.count()
                    if num_solves_chall - 1 == 0: 
                        first_blood = 1

                    # get team / user details
                    team = get_current_team()
                    user = get_current_user()

                    if first_blood:
                        # add first blood count
                        FirstBloods.addBlood(team.id, challenge)
            return result
        return wrapper

    def on_delete_submission(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            submission_id = kwargs.get('submission_id')
            if submission_id:

                submission = Submissions.query.filter_by(id=submission_id).first()
                
                if submission:
                    
                    submission_team_id = submission.team_id
                    submission_challenge = submission.challenge
                    team_bloods = get_team_bloods(submission_team_id)
                    blood = 0
                    if team_bloods:
                        # team exist in db
                        if len(team_bloods.challenges)>0:
                            # check if this submission is related to a first blood
                            for chal in team_bloods.challenges:
                                if submission_challenge.id == chal['id']:
                                    blood = 1
                                    break
                        else:
                            # the team related to this submission dont have any first bloods so just run delete normally..
                            result = f(*args, **kwargs)    
                        
                        # if it is related to a first blood then run the delete and make sure its successfull before updating first blood
                        if blood:
                            result = f(*args, **kwargs) 
                            if result.get("success"):
                                # need to query again because the wrapped function is closing the session
                                team_bloods = get_team_bloods(submission_team_id)
                                new_count = team_bloods.count - 1
                                team_bloods.count = new_count
                                db.session.commit()
                        else:
                            # not related to a first blood so delete it..
                            result = f(*args, **kwargs)    
        return wrapper

    app.view_functions['scoreboard.listing'] = scoreboard_view
    app.view_functions["api.challenges_challenge_attempt"] = challenge_attempt_decorator(app.view_functions["api.challenges_challenge_attempt"])
    Submission.delete = on_delete_submission(Submission.delete)
