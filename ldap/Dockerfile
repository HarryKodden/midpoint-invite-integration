FROM osixia/openldap:1.5.0

# Bake bootstrap LDIFs into the image. A bind mount cannot be used for
# .../bootstrap/ldif/custom because the entrypoint removes that path after import.
#
# - custom/     → domain data (OUs); applied after default bootstrap
# - 03-memberOf.ldif → replaces osixia's default (groupOfUniqueNames/uniqueMember)
#   with groupOfNames/member so MidPoint association deprovisioning works
COPY bootstrap/custom/ /container/service/slapd/assets/config/bootstrap/ldif/custom/
COPY bootstrap/03-memberOf.ldif /container/service/slapd/assets/config/bootstrap/ldif/03-memberOf.ldif
