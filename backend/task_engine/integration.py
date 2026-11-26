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
    message=str(exc) if isinstance(exc,(Conflict,LookupError,ValueError,PermissionError)) else 'Engine database unavailable; check the worker and PostgreSQL'
    return jsonify(ok=False,error=message),status


@blueprint.route(PREFIX)
def status():return jsonify(encode(store().status()))


@blueprint.route(PREFIX+'/runs',methods=['POST'])
def submit():
    if os.getenv('KUMO_ORCHESTRATION_RUNS_ENABLED','').lower()!='true':raise PermissionError('Execution controls are disabled')
    if os.getenv('KUMO_POSTGRES_ENGINE_PRIMARY','').lower()=='true':
        return jsonify(ok=False,error='Start a configured workflow through the Monitor'),410
    # Identity follows the app's trusted request context, never a payload actor.
    actor=current_app.extensions['task_prototype_actor_context']()
    return jsonify(store().submit(request.get_json(silent=True),actor.get('userName') or 'LOCAL_DEV')),202


@blueprint.route(PREFIX+'/runs/<run_id>')
def read(run_id):return jsonify(encode(store().read(run_id)))


@blueprint.route(PREFIX+'/runs/<run_id>/cancel',methods=['POST'])
def cancel(run_id):return jsonify(encode(store().cancel(run_id)))


@blueprint.route(PREFIX+'/runs/<run_id>/recover',methods=['POST'])
def recover(run_id):
    if os.getenv('KUMO_ORCHESTRATION_RUNS_ENABLED','').lower()!='true':raise PermissionError('Execution controls are disabled')
    actor=current_app.extensions['task_prototype_actor_context']()
    return jsonify(encode(store().recover(run_id,actor.get('userName') or 'LOCAL_DEV')))


@blueprint.route(PREFIX+'/schedules')
def schedules():
    from .schedules import Schedules
    return jsonify(encode(Schedules(store()).status()))


@blueprint.route(PREFIX+'/schedules',methods=['POST'])
def create_schedule():
    from .schedules import Schedules
    if os.getenv('KUMO_ORCHESTRATION_RUNS_ENABLED','').lower()!='true':raise PermissionError('Execution controls are disabled')
    body=request.get_json(silent=True)
    if os.getenv('KUMO_POSTGRES_ENGINE_PRIMARY','').lower()=='true' and not (isinstance(body,dict) and isinstance(body.get('target'),dict) and body['target'].get('graphId')):
        raise ValueError('Schedules must target a configured workflow')
    actor=current_app.extensions['task_prototype_actor_context']()
    return jsonify(encode(Schedules(store()).create(body,actor.get('userName') or 'LOCAL_DEV'))),201


@blueprint.route(PREFIX+'/schedules/<schedule_id>',methods=['PATCH'])
def update_schedule(schedule_id):
    from .schedules import Schedules
    if os.getenv('KUMO_ORCHESTRATION_RUNS_ENABLED','').lower()!='true':raise PermissionError('Execution controls are disabled')
    actor=current_app.extensions['task_prototype_actor_context']()
    return jsonify(encode(Schedules(store()).update(schedule_id,request.get_json(silent=True),actor.get('userName') or 'LOCAL_DEV')))


@blueprint.route(PREFIX+'/graphs')
def graphs():
    from .dependencies import Graphs
    return jsonify(encode(Graphs(store()).status()))


@blueprint.route(PREFIX+'/configuration',methods=['GET','PUT'])
@blueprint.route(PREFIX+'/configuration/<kind>',methods=['PUT'])
@blueprint.route(PREFIX+'/configuration/<kind>/<object_id>',methods=['PATCH'])
def configuration(kind=None,object_id=None):
    from .configuration import Configuration
    from .catalog import CatalogConfiguration,master
    db=store();service=CatalogConfiguration(db) if master(db) else Configuration(db)
    if request.method=='GET':
        return jsonify(encode(service.read()))
    actor=current_app.extensions['task_prototype_actor_context']().get('userName') or 'LOCAL_DEV'
    body=request.get_json(silent=True)
    if request.method=='PUT' and kind is None:result=service.dependencies(body,actor)
    elif request.method=='PUT' and kind=='job-order':result=service.orders(body,actor)
    elif request.method=='PATCH' and kind=='workflows':result=service.workflow(object_id,body,actor)
    elif request.method=='PATCH' and kind=='jobs':result=service.job(object_id,body,actor)
    else:raise LookupError('Unknown PostgreSQL configuration action')
    return jsonify(encode(result))


@blueprint.route(PREFIX+'/monitor')
def monitor():
    from .monitor import Monitor
    return jsonify(encode(Monitor(store()).payload()))


@blueprint.route(PREFIX+'/ready')
def ready():
    from .runtime import readiness
    result=readiness(store())
    return jsonify(result),200 if result['ok'] else 503


@blueprint.route(PREFIX+'/history')
def history():
    from .monitor import Monitor
    return jsonify(encode(Monitor(store()).history(request.args)))


@blueprint.route(PREFIX+'/workflows/<workflow_id>/runs/<run_id>/log')
def execution_log(workflow_id,run_id):
    from .monitor import Monitor
    return jsonify(encode(Monitor(store()).log(workflow_id,run_id,request.args.get('jobId'))))


@blueprint.route(PREFIX+'/graphs/<graph_id>/runs',methods=['POST'])
def start_graph(graph_id):
    from .dependencies import Graphs
    if os.getenv('KUMO_ORCHESTRATION_RUNS_ENABLED','').lower()!='true':raise PermissionError('Execution controls are disabled')
    actor=current_app.extensions['task_prototype_actor_context']()
    return jsonify(encode(Graphs(store()).start(graph_id,request.get_json(silent=True),actor.get('userName') or 'LOCAL_DEV'))),202


@blueprint.route(PREFIX+'/graphs/<graph_id>/plan')
def preview_graph(graph_id):
    from .monitor import Monitor
    include=request.args.get('includeChildren','true')
    if include not in ('true','false'):raise ValueError('includeChildren must be true or false')
    return jsonify(encode(Monitor(store()).preview(graph_id,request.args.get('workflowId'),include=='true')))


@blueprint.route(PREFIX+'/cascades/<cascade_id>')
def cascade(cascade_id):
    from .dependencies import Graphs
    return jsonify(encode(Graphs(store()).read(cascade_id)))


@blueprint.route(PREFIX+'/cascades/<cascade_id>/<action>',methods=['POST'])
def cascade_action(cascade_id,action):
    from .dependencies import Graphs
    if action not in ('cancel','retry-dispatch'):raise LookupError('Unknown dependency trial action')
    if os.getenv('KUMO_ORCHESTRATION_RUNS_ENABLED','').lower()!='true':raise PermissionError('Execution controls are disabled')
    service=Graphs(store())
    actor=current_app.extensions['task_prototype_actor_context']()
    user=actor.get('userName') or 'LOCAL_DEV'
    return jsonify(encode(service.cancel(cascade_id,actor=user) if action=='cancel' else service.retry_dispatch(cascade_id,actor=user)))


@blueprint.route(PREFIX+'/cascades/<cascade_id>/nodes/<node_id>/retry',methods=['POST'])
def retry_workflow(cascade_id,node_id):
    from .dependencies import Graphs
    if os.getenv('KUMO_ORCHESTRATION_RUNS_ENABLED','').lower()!='true':raise PermissionError('Execution controls are disabled')
    actor=current_app.extensions['task_prototype_actor_context']()
    return jsonify(encode(Graphs(store()).retry(cascade_id,node_id,actor.get('userName') or 'LOCAL_DEV'))),202


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

    @commands.command('import-wf1')
    @click.option('--apply',is_flag=True,help='Save the validated snapshot in PostgreSQL; Snowflake remains read-only')
    @click.option('--expected-revision',type=int,help='Required when replacing a changed graph snapshot')
    def import_wf1(apply,expected_revision):
        from .dependencies import Graphs
        from .wf1 import read_source,snapshot
        db=store();body,source=snapshot(read_source())
        if apply:click.echo(json.dumps(encode(Graphs(db).install(body,'LOCAL_DEV_CLI',source,expected_revision)),indent=2))
        else:click.echo(json.dumps(dict(ok=True,applied=False,graph=body,source=source),indent=2))

    @commands.command('import-catalog')
    @click.option('--apply',is_flag=True,help='Promote all DEV workflow/job definitions to PostgreSQL once')
    def import_complete_catalog(apply):
        from .catalog import compile_catalog,import_catalog,read_source,master
        db=store()
        if apply and master(db):raise click.ClickException('The PostgreSQL master is already installed; no Snowflake definitions were read')
        raw=read_source()
        graph=import_catalog(db,raw,'LOCAL_DEV_CLI')['definition'] if apply else compile_catalog(dict(key='dev-workflow-catalog',name='DEV workflow catalog',catalog=raw))
        click.echo(json.dumps(dict(ok=True,applied=apply,workflows=len(raw['workflows']),jobs=len(raw['jobs']),executableWorkflows=len(graph['nodes']),
            blockedWorkflows={w['WORKFLOW_NAME']:graph['executionIssues'][w['WORKFLOW_ID']] for w in raw['workflows'] if graph['executionIssues'][w['WORKFLOW_ID']]}),indent=2))

    @commands.command('schedule')
    @click.option('--once',is_flag=True,help='Evaluate at most one due schedule')
    def schedule(once):
        from .schedules import Scheduler
        worker=Scheduler(store())
        click.echo('DEV trial scheduler started; scheduling checks use PostgreSQL only')
        while True:
            try:
                processed=worker.step(execution_enabled=os.getenv('KUMO_ORCHESTRATION_RUNS_ENABLED','').lower()=='true')
            except Exception as exc:
                if once:raise click.ClickException('Scheduler paused: '+type(exc).__name__) from None
                click.echo('Scheduler paused; durable due times retained: '+type(exc).__name__,err=True)
                time.sleep(5)
                continue
            if once:return
            if not processed:time.sleep(1)

    @commands.command('work')
    @click.option('--once',is_flag=True,help='Perform at most one durable step')
    def work(once):
        from .dependencies import DependencyWorker
        from .snowflake_tasks import SnowflakeTasks
        from .worker import Worker
        db=store();worker=Worker(db,SnowflakeTasks(db.settings));dependencies=DependencyWorker(db)
        if not once and os.getenv('KUMO_TASK_ENGINE_RUNTIME_ENABLED','').lower()=='true':
            from threading import Thread
            from .runtime import worker_heartbeat
            def heartbeat():
                while True:
                    try:worker_heartbeat(db,dependencies.owner)
                    except Exception as exc:click.echo('Worker heartbeat unavailable: '+type(exc).__name__,err=True)
                    time.sleep(5)
            Thread(target=heartbeat,name='native-worker-heartbeat',daemon=True).start()
        click.echo('DEV prototype worker started; only recorded disposable attempts are eligible')
        while True:
            try:
                enabled=os.getenv('KUMO_TASK_ENGINE_WORKER_ENABLED','true').lower()=='true'
                dispatched=dependencies.step(execution_enabled=enabled and os.getenv('KUMO_ORCHESTRATION_RUNS_ENABLED','').lower()=='true')
                processed=(worker.step() if enabled else False) or dispatched
            except Exception as exc:
                if once:raise click.ClickException('Worker paused: '+type(exc).__name__) from None
                click.echo('Worker paused; PostgreSQL recovery will retry: '+type(exc).__name__,err=True)
                time.sleep(5)
                continue
            if once:return
            if not processed:time.sleep(1)  # PostgreSQL-only idle check; never touches Snowflake.
