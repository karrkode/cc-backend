# Email and Microsoft 365 records, copied exactly from the GoDaddy zone export of
# 2026-10-17. Change these only when Microsoft or GoDaddy (the mail provider) asks.
#
# Deliberately not copied: _domainconnect (GoDaddy's one-click setup; only works while
# GoDaddy hosts the DNS), the Netlify A/CNAME records (replaced in dns.tf), and the
# aws. NS delegation used while staging.

locals {
  mail_records = {
    "MX @" = {
      name = "", type = "MX"
      records = [
        "0 mx1-usg2.ppe-hosted.com.",
        "0 mx2-usg2.ppe-hosted.com.",
        "0 mx3-usg2.ppe-hosted.com.",
      ]
    }
    "TXT @" = {
      name = "", type = "TXT"
      records = [
        "NETORGFT21209736.onmicrosoft.com",
        "v=spf1 include:_spf-usg2.ppe-hosted.com include:secureserver.net ~all",
      ]
    }
    "TXT _dmarc" = {
      name    = "_dmarc", type = "TXT"
      records = ["v=DMARC1; p=quarantine; adkim=r; aspf=r; rua=mailto:dmarc_rua@onsecureserver.net;"]
    }
    "CNAME autodiscover"         = { name = "autodiscover", type = "CNAME", records = ["autodiscover.outlook.com."] }
    "CNAME email"                = { name = "email", type = "CNAME", records = ["email.secureserver.net."] }
    "CNAME lyncdiscover"         = { name = "lyncdiscover", type = "CNAME", records = ["webdir.online.lync.com."] }
    "CNAME msoid"                = { name = "msoid", type = "CNAME", records = ["clientconfig.microsoftonline-p.net."] }
    "CNAME sip"                  = { name = "sip", type = "CNAME", records = ["sipdir.online.lync.com."] }
    "CNAME selector1._domainkey" = { name = "selector1._domainkey", type = "CNAME", records = ["selector1-chiculturecrew-com._domainkey.netorgft21209736.p-v1.dkim.mail.microsoft."] }
    "CNAME selector2._domainkey" = { name = "selector2._domainkey", type = "CNAME", records = ["selector2-chiculturecrew-com._domainkey.netorgft21209736.p-v1.dkim.mail.microsoft."] }
    "SRV _sip._tls"              = { name = "_sip._tls", type = "SRV", records = ["100 1 443 sipdir.online.lync.com."] }
    "SRV _sipfederationtls._tcp" = { name = "_sipfederationtls._tcp", type = "SRV", records = ["100 1 5061 sipfed.online.lync.com."] }
  }
}

resource "aws_route53_record" "mail" {
  for_each = local.mail_records
  zone_id  = aws_route53_zone.main.zone_id
  name     = each.value.name == "" ? var.zone_name : "${each.value.name}.${var.zone_name}"
  type     = each.value.type
  ttl      = 3600
  records  = each.value.records
}
