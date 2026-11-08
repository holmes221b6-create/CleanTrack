from datetime import datetime, timezone
from app.extensions import db
def utc_now(): return datetime.now(timezone.utc)
class OperationalIncident(db.Model):
 __tablename__="operational_incidents"
 id=db.Column(db.Integer,primary_key=True)
 organization_id=db.Column(db.Integer,db.ForeignKey("organizations.id"),nullable=False,index=True)
 team_id=db.Column(db.Integer,db.ForeignKey("teams.id"),nullable=False,index=True)
 reported_by_id=db.Column(db.Integer,db.ForeignKey("users.id"),nullable=False)
 operation_id=db.Column(db.Integer,db.ForeignKey("operations.id"),nullable=True,index=True)
 location_id=db.Column(db.Integer,db.ForeignKey("locations.id"),nullable=True)
 zone_id=db.Column(db.Integer,db.ForeignKey("zones.id"),nullable=True)
 title=db.Column(db.String(180),nullable=False); description=db.Column(db.Text,nullable=False)
 category=db.Column(db.String(40),nullable=False,default="other"); severity=db.Column(db.String(20),nullable=False,default="medium")
 status=db.Column(db.String(20),nullable=False,default="open",index=True); resolution=db.Column(db.Text)
 resolved_at=db.Column(db.DateTime(timezone=True)); created_at=db.Column(db.DateTime(timezone=True),nullable=False,default=utc_now,index=True)
 updated_at=db.Column(db.DateTime(timezone=True),nullable=False,default=utc_now,onupdate=utc_now)
 reporter=db.relationship("User",foreign_keys=[reported_by_id]); operation=db.relationship("Operation"); location=db.relationship("Location"); zone=db.relationship("Zone")
 def to_dict(self):
  return {"id":self.id,"title":self.title,"description":self.description,"category":self.category,"severity":self.severity,"status":self.status,"resolution":self.resolution,"operation_id":self.operation_id,"operation_title":self.operation.title if self.operation else None,"location_id":self.location_id,"location_name":self.location.name if self.location else None,"zone_id":self.zone_id,"zone_name":self.zone.name if self.zone else None,"reported_by":self.reporter.full_name if self.reporter else None,"created_at":self.created_at.isoformat() if self.created_at else None,"updated_at":self.updated_at.isoformat() if self.updated_at else None,"resolved_at":self.resolved_at.isoformat() if self.resolved_at else None}
class ShiftHandover(db.Model):
 __tablename__="supervisor_shift_handovers"
 id=db.Column(db.Integer,primary_key=True)
 organization_id=db.Column(db.Integer,db.ForeignKey("organizations.id"),nullable=False,index=True); team_id=db.Column(db.Integer,db.ForeignKey("teams.id"),nullable=False,index=True)
 created_by_id=db.Column(db.Integer,db.ForeignKey("users.id"),nullable=False); acknowledged_by_id=db.Column(db.Integer,db.ForeignKey("users.id"))
 shift_label=db.Column(db.String(100),nullable=False); outgoing_at=db.Column(db.DateTime(timezone=True),nullable=False); incoming_at=db.Column(db.DateTime(timezone=True))
 summary=db.Column(db.Text,nullable=False); outstanding_items=db.Column(db.JSON,nullable=False,default=list); status=db.Column(db.String(20),nullable=False,default="draft",index=True)
 acknowledged_at=db.Column(db.DateTime(timezone=True)); created_at=db.Column(db.DateTime(timezone=True),nullable=False,default=utc_now,index=True)
 creator=db.relationship("User",foreign_keys=[created_by_id]); acknowledger=db.relationship("User",foreign_keys=[acknowledged_by_id])
 def to_dict(self):
  return {"id":self.id,"shift_label":self.shift_label,"outgoing_at":self.outgoing_at.isoformat() if self.outgoing_at else None,"incoming_at":self.incoming_at.isoformat() if self.incoming_at else None,"summary":self.summary,"outstanding_items":self.outstanding_items or [],"status":self.status,"created_by":self.creator.full_name if self.creator else None,"created_by_id":self.created_by_id,"acknowledged_by":self.acknowledger.full_name if self.acknowledger else None,"acknowledged_at":self.acknowledged_at.isoformat() if self.acknowledged_at else None,"created_at":self.created_at.isoformat() if self.created_at else None}
class QualityInspection(db.Model):
 __tablename__="quality_inspections"
 id=db.Column(db.Integer,primary_key=True)
 organization_id=db.Column(db.Integer,db.ForeignKey("organizations.id"),nullable=False,index=True); team_id=db.Column(db.Integer,db.ForeignKey("teams.id"),nullable=False,index=True)
 created_by_id=db.Column(db.Integer,db.ForeignKey("users.id"),nullable=False); operation_id=db.Column(db.Integer,db.ForeignKey("operations.id"),nullable=True,index=True)
 location_id=db.Column(db.Integer,db.ForeignKey("locations.id"),nullable=True); zone_id=db.Column(db.Integer,db.ForeignKey("zones.id"),nullable=True)
 title=db.Column(db.String(180),nullable=False); checklist=db.Column(db.JSON,nullable=False,default=list)
 status=db.Column(db.String(20),nullable=False,default="planned",index=True); outcome=db.Column(db.String(20)); findings=db.Column(db.Text); inspected_at=db.Column(db.DateTime(timezone=True))
 created_at=db.Column(db.DateTime(timezone=True),nullable=False,default=utc_now,index=True)
 creator=db.relationship("User",foreign_keys=[created_by_id]); operation=db.relationship("Operation"); location=db.relationship("Location"); zone=db.relationship("Zone")
 def to_dict(self):
  return {"id":self.id,"title":self.title,"checklist":self.checklist or [],"status":self.status,"outcome":self.outcome,"findings":self.findings,"operation_id":self.operation_id,"operation_title":self.operation.title if self.operation else None,"location_id":self.location_id,"location_name":self.location.name if self.location else None,"zone_id":self.zone_id,"zone_name":self.zone.name if self.zone else None,"created_by":self.creator.full_name if self.creator else None,"inspected_at":self.inspected_at.isoformat() if self.inspected_at else None,"created_at":self.created_at.isoformat() if self.created_at else None}
