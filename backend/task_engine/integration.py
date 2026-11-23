"""Prototype routes and worker CLI in the existing Flask application."""
from datetime import datetime
import json
import os
import time
import uuid

import click
from flask import Blueprint, current_app, jsonify, request

from .settings import Settings
from .store import Conflict, Store

PREFIX='/api/orchestration/task-engine'
blueprint=Blueprint('postgres_task_engine',__name__)


def store():return Store(Settings.from_environment())


def encode(value):
    if isinstance(value,dict):return {k:encode(v) for k,v in value.items()}
    if isinstance(value,list):return [encode(v) for v in value]
    if isinstance(value,uuid.UUID):return str(value)
    if isinstance(value,datetime):return value.isoformat()
    return value


@blueprint.errorhandler(Exception)
def error(exc):
    status=409 if isinstance(exc,Conflict) else 404 if isinstance(exc,LookupError) else 400 if isinstance(exc,ValueError) else 503
    # Do not return PostgreSQL connection errors/DSNs to the browser.
    message=str(exc) if isinstance(exc,(Conflict,LookupError,ValueError,PermissionError)) else 'Prototype database unavailable; check the local worker and PostgreSQL'
    return jsonify(ok=False,error=message),status


@blueprint.route(PREFIX)
def status():return jsonify(encode(store().status()))


@blueprint.route(PREFIX+'/runs',methods=['POST'])
def submit():
    if os.getenv('KUMO_ORCHESTRATION_RUNS_ENABLED','').lower()!='true':raise PermissionError('Execution controls are disabled')
    # Identity follows the app's trusted request context, never a payload actor.
    actor=current_app.extensions['task_prototype_actor_context']()
    return jsonify(store().submit(request.get_json(silent=True),actor.get('userName') or 'LOCAL_DEV')),202


@blueprint.route(PREFIX+'/runs/<run_id>')
def read(run_id):return jsonify(encode(store().read(run_id)))


@blueprint.route(PREFIX+'/runs/<run_id>/cancel',methods=['POST'])
def cancel(run_id):return jsonify(encode(store().cancel(run_id)))


def register(app,actor_context):
    app.register_blueprint(blueprint)
    app.extensions['task_prototype_actor_context']=actor_context

    @app.cli.group('task-engine')
    def commands():
        """Opt-in DEV PostgreSQL/native-task prototype."""

    @commands.command('init')
    def initialize():
        store().initialize()
        click.echo('PostgreSQL task prototype initialized; Snowflake unchanged')

    @commands.command('status')
    def show():click.echo(json.dumps(encode(store().status()),indent=2))

    @commands.command('submit')
    @click.argument('plan',type=click.Path(exists=True,dir_okay=False))
    def enqueue(plan):
        with open(plan) as stream:body=json.load(stream)
        click.echo(json.dumps(store().submit(body,'LOCAL_DEV_CLI'),indent=2))

    @commands.command('work')
    @click.option('--once',is_flag=True,help='Perform at most one durable step')
    def work(once):
        from .snowflake_tasks import SnowflakeTasks
        from .worker import Worker
        db=store();worker=Worker(db,SnowflakeTasks(db.settings))
        click.echo('DEV prototype worker started; only recorded disposable attempts are eligible')
        while True:
            try:
                processed=worker.step()
            except Exception as exc:
                if once:raise click.ClickException('Worker paused: '+type(exc).__name__) from None
                click.echo('Worker paused; PostgreSQL recovery will retry: '+type(exc).__name__,err=True)
                time.sleep(5)
                continue
            if once:return
            if not processed:time.sleep(1)  # PostgreSQL-only idle check; never touches Snowflake.
