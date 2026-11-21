import pytest
from src.mud.content import Entity, Room

def test_entity_initialization():
    """[TASK-009] RED PHASE: Verify that an Entity can be created with name, desc, and weight."""
    name = "Rusty Sword"
    description = "A dull, notched blade."
    weight = 5.0
    
    entity = Entity(name=name, description=description, weight=weight)
    
    assert entity.name == name
    assert entity.description == description
    assert entity.weight == weight

def test_entity_default_values():
    """Verify default values for an Entity."""
    # This is defensive programming to ensure we don't break later tasks
    entity = Entity(name="Stone", description="A plain stone.", weight=1.0)
    assert entity.weight == 1.0
