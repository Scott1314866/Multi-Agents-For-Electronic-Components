"""Shared native scheduling: FIFO, distinct devices and cancellation ownership."""
import asyncio
import threading

import pytest

from backend.core import cadence_scheduler as C


@pytest.fixture
def scheduler(monkeypatch):
    scheduler=C.CadenceScheduler()
    monkeypatch.setattr(C, 'device_key', lambda mode: mode)
    yield scheduler
    for executor in scheduler._executors.values():
        executor.shutdown(wait=True)


def test_capture_and_allegro_share_fifo_and_hide_other_tenants(scheduler):
    started,release=threading.Event(),threading.Event()
    order=[]
    def first():
        order.append('capture');started.set();assert release.wait(3)
    def second():
        order.append('allegro')
    async def scenario():
        first_task=asyncio.create_task(scheduler.run(first, mode='ssh',kind='symbol',drawing_id='a',tenant_id='one'))
        assert await asyncio.to_thread(started.wait,1)
        second_task=asyncio.create_task(scheduler.run(second,mode='ssh',kind='pcb',drawing_id='b',tenant_id='two'))
        await asyncio.sleep(.01)
        assert scheduler.snapshot('one') == [{'mode':'ssh','kind':'symbol','drawing_id':'a','state':'running'}]
        assert scheduler.snapshot('two')[0]['state']=='queued' and order==['capture']
        release.set();await asyncio.gather(first_task,second_task)
        assert order==['capture','allegro'] and not scheduler.snapshot('one')
    asyncio.run(scenario())


def test_different_devices_can_run_independently(scheduler):
    started,release=threading.Event(),threading.Event()
    def local():
        started.set();assert release.wait(3)
    async def scenario():
        task=asyncio.create_task(scheduler.run(local,mode='local',kind='symbol'))
        assert await asyncio.to_thread(started.wait,1)
        assert await scheduler.run(lambda:'remote',mode='ssh',kind='pcb')=='remote'
        release.set();await task
    asyncio.run(scenario())


def test_cancellation_retains_slot_until_native_process_finishes(scheduler):
    started,release=threading.Event(),threading.Event()
    events=[]
    def work():
        started.set();assert release.wait(3);events.append('finished')
    async def scenario():
        owner=asyncio.create_task(scheduler.run(work,mode='local',kind='pcb',tenant_id='a'))
        assert await asyncio.to_thread(started.wait,1)
        owner.cancel();await asyncio.sleep(.01)
        assert not owner.done() and scheduler.snapshot('a')[0]['state']=='running'
        release.set()
        with pytest.raises(asyncio.CancelledError): await owner
        assert events==['finished'] and not scheduler.snapshot('a')
    asyncio.run(scenario())
